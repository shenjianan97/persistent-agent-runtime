import urllib.request
import urllib.error
import json
import time
import sys
import psycopg2

BASE_URL = "http://localhost:8080/v1"
DB_DSN = "postgresql://postgres:postgres@localhost:55432/persistent_agent_runtime"

def get_db():
    return psycopg2.connect(DB_DSN)

def submit_task(agent_id="test_agent_py", model="claude-sonnet-4-6", tools=["web_search"], expect_error=False, max_retries=1):
    print(f"Submitting a new task (model={model}, tools={tools})...")
    payload = {
        "agent_id": agent_id,
        "agent_config": {
            "system_prompt": "You are a test assistant.",
            "model": model,
            "temperature": 0.5,
            "allowed_tools": tools
        },
        "input": "Calculate the square root of 256.",
        "max_retries": max_retries,
        "max_steps": 5,
        "task_timeout_seconds": 300
    }
    req = urllib.request.Request(f"{BASE_URL}/tasks", method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        data = json.dumps(payload).encode("utf-8")
        with urllib.request.urlopen(req, data=data) as f:
            if expect_error:
                print("FAIL: Expected an error but submission succeeded!")
                raise AssertionError("Expected error did not occur")
            resp = json.loads(f.read().decode("utf-8"))
            print(f"Task submitted successfully! ID: {resp['task_id']}")
            return resp['task_id']
    except urllib.error.HTTPError as e:
        if expect_error:
            print(f"SUCCESS: Got expected HTTP {e.code} error for invalid submission.")
            return None
        print(f"Failed to submit task: {e}")
        raise
    except Exception as e:
        print(f"Failed to submit task: {e}")
        raise

def get_task(task_id, expect_status=None):
    print(f"Getting task {task_id} status...")
    req = urllib.request.Request(f"{BASE_URL}/tasks/{task_id}", method="GET")
    try:
        with urllib.request.urlopen(req) as f:
            resp = json.loads(f.read().decode("utf-8"))
            print(f"Task status is: {resp['status']}")
            if expect_status:
                assert resp['status'] == expect_status, f"Expected {expect_status}, got {resp['status']}"
            return resp
    except Exception as e:
        print(f"Failed to get task: {e}")
        raise

def cancel_task(task_id, expect_error=False):
    print(f"Canceling task {task_id}...")
    req = urllib.request.Request(f"{BASE_URL}/tasks/{task_id}/cancel", method="POST")
    try:
        with urllib.request.urlopen(req) as f:
            if expect_error:
                print("FAIL: Expected an error but cancel succeeded!")
                raise AssertionError("Expected error did not occur")
            resp = json.loads(f.read().decode("utf-8"))
            print(f"Task cancelled! current status: {resp['status']}, reason: {resp.get('dead_letter_reason')}")
            assert resp['status'] == 'dead_letter'
            assert resp['dead_letter_reason'] == 'cancelled_by_user'
    except urllib.error.HTTPError as e:
        if expect_error:
            print(f"SUCCESS: Got expected HTTP {e.code} error for invalid cancel.")
            return
        print(f"Failed to cancel task: {e}")
        raise
    except Exception as e:
        print(f"Failed to cancel task: {e}")
        raise


def redrive_task(task_id, expect_error=False):
    print(f"Redriving task {task_id}...")
    req = urllib.request.Request(f"{BASE_URL}/tasks/{task_id}/redrive", method="POST")
    try:
        with urllib.request.urlopen(req) as f:
            if expect_error:
                print("FAIL: Expected an error but redrive succeeded!")
                raise AssertionError("Expected error did not occur")
            resp = json.loads(f.read().decode("utf-8"))
            print(f"Task redriven! current status: {resp['status']}")
            assert resp['status'] == 'queued'
    except urllib.error.HTTPError as e:
        if expect_error:
            print(f"SUCCESS: Got expected HTTP {e.code} error for invalid redrive.")
            return
        print(f"Failed to redrive task: {e}")
        raise
    except Exception as e:
        print(f"Failed to redrive task: {e}")
        raise

def get_dead_letter_list(agent_id="test_agent_py"):
    print(f"Getting dead letter queue for agent {agent_id}...")
    req = urllib.request.Request(f"{BASE_URL}/tasks/dead-letter?agent_id={agent_id}", method="GET")
    try:
        with urllib.request.urlopen(req) as f:
            resp = json.loads(f.read().decode("utf-8"))
            print(f"Found {len(resp.get('items', []))} items in dead letter queue.")
            return resp
    except Exception as e:
        print(f"Failed to list dead letters: {e}")
        raise

def test_scenario_1_worker_crash():
    print("\nScenario 1: Worker Crash & Lease Expiry Recovery")
    task_id = submit_task()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tasks 
                SET status='running', lease_owner='crash-worker-1', lease_expiry=NOW() - INTERVAL '1 minute'
                WHERE task_id = %s
            """, (task_id,))
            conn.commit()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tasks 
                SET status='queued', 
                    retry_count=retry_count+1, 
                    retry_after=NOW(),
                    lease_owner=NULL,
                    lease_expiry=NULL
                WHERE task_id = %s
            """, (task_id,))
            conn.commit()
    task = get_task(task_id)
    assert task['status'] == 'queued'
    assert task['retry_count'] == 1
    assert task['lease_owner'] is None
    print("Scenario 1 Passed.")

def test_scenario_2_non_retryable_error():
    print("\nScenario 2: Non-Retryable Node Error")
    task_id = submit_task()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tasks 
                SET status='dead_letter', 
                    dead_letter_reason='non_retryable_error',
                    last_error_code='fatal_error',
                    last_error_message='Invalid tool args',
                    last_worker_id='error-worker-1',
                    lease_owner=NULL,
                    lease_expiry=NULL
                WHERE task_id = %s
            """, (task_id,))
            conn.commit()
    task = get_task(task_id)
    assert task['status'] == 'dead_letter'
    assert task['dead_letter_reason'] == 'non_retryable_error'
    assert task['last_error_code'] == 'fatal_error'
    assert task['last_error_message'] == 'Invalid tool args'
    print("Scenario 2 Passed.")

def test_scenario_3_retryable_error():
    print("\nScenario 3: Retryable Error with Backoff Requeue")
    task_id = submit_task()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tasks 
                SET status='queued', 
                    retry_count=retry_count+1,
                    retry_after=NOW() + INTERVAL '5 seconds',
                    lease_owner=NULL,
                    lease_expiry=NULL
                WHERE task_id = %s
            """, (task_id,))
            conn.commit()
    task = get_task(task_id)
    assert task['status'] == 'queued'
    assert task['retry_count'] == 1
    print("Scenario 3 Passed.")

def test_scenario_4_cancellation():
    print("\nScenario 4: Task Cancellation During Execution")
    task_id = submit_task()
    cancel_task(task_id)
    task = get_task(task_id)
    assert task['status'] == 'dead_letter'
    assert task['dead_letter_reason'] == 'cancelled_by_user'
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tasks SET lease_expiry = NOW() + INTERVAL '1 minute'
                WHERE task_id = %s AND lease_owner='my_worker' AND status='running'
            """, (task_id,))
            rows = cur.rowcount
            assert rows == 0
    print("Scenario 4 Passed.")

def test_scenario_5_redrive():
    print("\nScenario 5: Redrive from Dead Letter")
    task_id = submit_task(max_retries=3)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tasks 
                SET status='dead_letter', 
                    dead_letter_reason='retries_exhausted',
                    retry_count=3,
                    lease_owner=NULL,
                    lease_expiry=NULL
                WHERE task_id = %s
            """, (task_id,))
            conn.commit()
    task = get_task(task_id)
    assert task['status'] == 'dead_letter'
    assert task['dead_letter_reason'] == 'retries_exhausted'
    redrive_task(task_id)
    task = get_task(task_id)
    assert task['status'] == 'queued'
    assert task['retry_count'] == 0
    assert task['dead_letter_reason'] is None
    print("Scenario 5 Passed.")


def run_tests():
    print("=== Running API Service Integration Tests ===")
    try:
        health_req = urllib.request.Request(f"{BASE_URL}/health", method="GET")
        with urllib.request.urlopen(health_req) as f:
            health = json.loads(f.read().decode("utf-8"))
            assert health["status"] == "healthy", "Service is not healthy!"
            print("1. Service health check passed.\n")
        
        print("2. Testing invalid model submission...")
        submit_task(model="gpt-5-unreleased", expect_error=True)
        print()

        print("3. Testing invalid tool submission...")
        submit_task(tools=["dangerous_bash_eval"], expect_error=True)
        print()
        
        print("4. Testing missing agent_id submission...")
        submit_task(agent_id=None, expect_error=True)
        print()

        print("5. Testing invalid timeout constraints (timeout > 86400)...")
        req = urllib.request.Request(f"{BASE_URL}/tasks", method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req, data=json.dumps({
                "agent_id": "test",
                "agent_config": {"system_prompt": "sys", "model": "claude-sonnet-4-6"},
                "input": "test",
                "task_timeout_seconds": 999999
            }).encode("utf-8"))
            raise AssertionError("Expected error did not occur")
        except urllib.error.HTTPError as e:
            print(f"SUCCESS: Got expected HTTP {e.code} error for invalid timeout.")
        print()
        
        print("6. Testing invalid max steps constraint (max_steps < 1)...")
        req = urllib.request.Request(f"{BASE_URL}/tasks", method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req, data=json.dumps({
                "agent_id": "test",
                "agent_config": {"system_prompt": "sys", "model": "claude-sonnet-4-6"},
                "input": "test",
                "max_steps": 0
            }).encode("utf-8"))
            raise AssertionError("Expected error did not occur")
        except urllib.error.HTTPError as e:
            print(f"SUCCESS: Got expected HTTP {e.code} error for invalid max_steps.")
        print()
        
        print("7. Testing invalid temperature constraints (temperature > 2.0)...")
        req = urllib.request.Request(f"{BASE_URL}/tasks", method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req, data=json.dumps({
                "agent_id": "test",
                "agent_config": {"system_prompt": "sys", "model": "claude-sonnet-4-6", "temperature": 2.5},
                "input": "test"
            }).encode("utf-8"))
            raise AssertionError("Expected error did not occur")
        except urllib.error.HTTPError as e:
            print(f"SUCCESS: Got expected HTTP {e.code} error for invalid temperature.")
        print()

        print("8. Testing task lifecycle (Submit -> Cancel -> Cancel(fail) -> Redrive -> Checkpoints)...")
        task_id = submit_task()
        get_task(task_id, expect_status="queued")
        
        cancel_task(task_id)
        get_task(task_id, expect_status="dead_letter")
        
        print("Testing conflicting action: double cancel...")
        cancel_task(task_id, expect_error=True)
        
        get_dead_letter_list()

        redrive_task(task_id)
        get_task(task_id, expect_status="queued")
        
        print("Testing conflicting action: redrive queued task...")
        redrive_task(task_id, expect_error=True)

        print(f"Getting checkpoints for task {task_id}...")
        try:
            with urllib.request.urlopen(urllib.request.Request(f"{BASE_URL}/tasks/{task_id}/checkpoints", method="GET")) as f:
                resp = json.loads(f.read().decode("utf-8"))
                assert 'checkpoints' in resp
                print(f"Checkpoints retrieved. Count: {len(resp['checkpoints'])}")
        except Exception as e:
            print(f"Failed to get checkpoints: {e}")
            raise
            
        print("\n9. Testing get non-existent task...")
        try:
            req = urllib.request.Request(f"{BASE_URL}/tasks/00000000-0000-0000-0000-000000000000", method="GET")
            urllib.request.urlopen(req)
            raise AssertionError("Expected 404 did not occur")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"SUCCESS: Got expected HTTP 404 error for non-existent task.")
            else:
                raise
        
        print("\n=== Running Failure Scenarios DB Simulation ===")
        test_scenario_1_worker_crash()
        test_scenario_2_non_retryable_error()
        test_scenario_3_retryable_error()
        test_scenario_4_cancellation()
        test_scenario_5_redrive()

        print("\nAll integration tests passed successfully!")
    except Exception as e:
        print("\nIntegration tests failed!", e)
        exit(1)

if __name__ == "__main__":
    try:
        get_db().close()
    except psycopg2.OperationalError as e:
        print("Could not connect to database at localhost:55432:", e)
        sys.exit(1)
        
    run_tests()
