FROM public.ecr.aws/lambda/python:3.12

WORKDIR /var/task

COPY pyproject.toml ./
COPY main.py ./

RUN pip install --no-cache-dir .

CMD ["main.lambda_handler"]
