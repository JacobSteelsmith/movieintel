# movieintel enrichment pipeline (CDK)

This CDK Python app defines the Task 6 enrichment pipeline stack
(`MovieIntelPipelineStack`): an on-demand DynamoDB single table with a GSI1, an S3
Vectors bucket + index built from the native `aws_s3vectors` L1 constructs, a Bedrock
Guardrail, the seven handler Lambdas (the six pipeline steps plus Evaluate), and the
Step Functions state machine that orchestrates them.

## CLI invocation

There is no global `cdk` on PATH; invoke the CDK CLI through `npx` from this directory.
The `app` command in `cdk.json` runs the entry point inside the project's uv virtualenv.

```sh
cd infra
npx aws-cdk@2 synth     # synthesize the CloudFormation template (does not deploy)
npx aws-cdk@2 deploy    # deploy the stack (run only when you intend to provision AWS)
```

Synth writes `infra/cdk.out/`, which is gitignored.

## Stop contracts

The stack fails loudly at synth time rather than degrading silently in two cases:

- **S3 Vectors is native, with no OpenSearch substitution.** The vector store is built
  from `aws_cdk.aws_s3vectors`. If that module cannot be imported the stack raises
  `S3VectorsUnavailableError` (see `constants.import_s3vectors`) and synth stops. We do
  not fall back to an OpenSearch collection.
- **The source databases must exist.** The read-only `_sqlite/movies.db` and
  `_sqlite/ratings.db` are packaged as a Lambda layer. If either is absent,
  `constants.check_source_dbs` raises `MissingSourceDatabaseError` at synth time.

## Handlers

The Lambda code lives under `infra/handlers/<step>/handler.py` (physical dir name kept as
`handlers/` to avoid the Python `lambda` keyword collision). The handler code asset is the
`infra/` directory (excluding the CDK app/stack modules, `cdk.out/`, caches, and
`_sqlite/`), so the `handlers` package deploys as a top-level package at `/var/task/handlers/`
and each Lambda uses the handler string `handlers.<step>.handler.handler`. That keeps the
runtime import path identical to the one the tests use (`from handlers.shared import ...`).

The handlers thinly wrap the `movieintel` package, which is shipped to the Lambdas as a
shared layer. The layer is bundled so `movieintel` and `pydantic` sit under `python/`
(mounted at `/opt/python`) and resolve on the Lambda import path; `boto3`/`botocore` come
from the Lambda runtime. The handlers add no business logic.
