# CloudTrim engine — real boto3 + Terraform audit engine, endpoint-agnostic.
# Demo target: LocalStack (docker-compose). Live target: real AWS.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl unzip \
      fonts-dejavu-core fonts-freefont-ttf \
    && rm -rf /var/lib/apt/lists/*

ARG TF_VERSION=1.9.8
RUN curl -sSLo /tmp/tf.zip \
      "https://releases.hashicorp.com/terraform/${TF_VERSION}/terraform_${TF_VERSION}_linux_amd64.zip" \
    && unzip -oq /tmp/tf.zip -d /usr/local/bin \
    && chmod +x /usr/local/bin/terraform \
    && rm /tmp/tf.zip \
    && terraform version

WORKDIR /app
COPY requirements.txt pyproject.toml ./
COPY cloudtrim ./cloudtrim
COPY terraform ./terraform
RUN pip install --no-cache-dir .

ENV AWS_ENDPOINT_URL=http://localstack:4566 \
    AWS_REGION=us-west-2 \
    TERRAFORM_BIN=terraform \
    PORT=8000

EXPOSE 8000
CMD ["cloudtrim", "serve", "--host", "0.0.0.0", "--port", "8000"]
