# CloudTrim demo target — moto AWS API emulator, server mode.
#
# Why moto (and not LocalStack community): the demo seed and remediation
# pipeline exercise ELBv2, S3 lifecycle configuration and CloudWatch Logs —
# services that are Pro-only in LocalStack Community. moto implements the
# full AWS API surface the engine touches at no charge, and it is the exact
# environment the engine's end-to-end numbers were verified against
# (moto 5.2.3: 99 findings, 65/65 verified remediation actions, 31.9%
# measured reduction). The engine treats it like any AWS-compatible
# endpoint — point AWS_ENDPOINT_URL at LocalStack Pro or real AWS and the
# identical code path runs.
FROM python:3.12-slim

RUN pip install --no-cache-dir "moto[server]==5.2.3"

ENV DEFAULT_REGION=us-west-2 \
    AWS_ACCESS_KEY_ID=test \
    AWS_SECRET_ACCESS_KEY=test

EXPOSE 4566
CMD ["moto_server", "-H", "0.0.0.0", "-p", "4566"]
