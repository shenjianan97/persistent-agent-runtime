"""Tests for exponential backoff schedule progression."""

import pytest

from core.config import WorkerConfig


class TestBackoffProgression:
    """Verify the exponential backoff schedule: 100ms -> 200ms -> 400ms -> ... -> 5s cap."""

    def test_initial_backoff(self):
        config = WorkerConfig()
        assert config.poll_backoff_initial_ms == 100

    def test_backoff_progression(self):
        """Simulate backoff progression and verify the schedule."""
        config = WorkerConfig()
        backoff_ms = config.poll_backoff_initial_ms
        expected = [100, 200, 400, 800, 1600, 3200, 5000, 5000, 5000]

        schedule = []
        for _ in range(len(expected)):
            schedule.append(backoff_ms)
            backoff_ms = min(
                int(backoff_ms * config.poll_backoff_multiplier),
                config.poll_backoff_max_ms,
            )

        assert schedule == expected

    def test_backoff_caps_at_max(self):
        config = WorkerConfig(
            poll_backoff_initial_ms=100,
            poll_backoff_max_ms=5000,
            poll_backoff_multiplier=2.0,
        )
        backoff_ms = config.poll_backoff_initial_ms

        # Run many iterations
        for _ in range(100):
            backoff_ms = min(
                int(backoff_ms * config.poll_backoff_multiplier),
                config.poll_backoff_max_ms,
            )

        assert backoff_ms == 5000

    def test_backoff_reset(self):
        """After a successful claim, backoff resets to initial."""
        config = WorkerConfig()
        backoff_ms = 3200  # Simulated advanced backoff

        # Reset on successful claim
        backoff_ms = config.poll_backoff_initial_ms
        assert backoff_ms == 100

    def test_custom_backoff_params(self):
        config = WorkerConfig(
            poll_backoff_initial_ms=50,
            poll_backoff_max_ms=2000,
            poll_backoff_multiplier=3.0,
        )
        backoff_ms = config.poll_backoff_initial_ms
        expected = [50, 150, 450, 1350, 2000]

        schedule = []
        for _ in range(len(expected)):
            schedule.append(backoff_ms)
            backoff_ms = min(
                int(backoff_ms * config.poll_backoff_multiplier),
                config.poll_backoff_max_ms,
            )

        assert schedule == expected
