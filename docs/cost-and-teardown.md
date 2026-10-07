# Cost Model and Teardown

This document records the MovieIntel cost model (REQ-X-5.1/.2) and a teardown verification
checklist (REQ-X-5.3). The architecture is built to cost near-zero when idle, with cost
driven almost entirely by active enrichment and query traffic. This task does NOT run
`cdk destroy`; the teardown section lists the destroy order and non-destructive
verification commands only.

## Cost model

### Idle cost (near-zero)

When no enrichment run and no `/query` traffic is happening, the stack carries essentially
no compute cost. The only standing charges are storage of data already written.

| Resource | Idle cost driver | Idle cost |
| --- | --- | --- |
| DynamoDB (`MovieIntel`, on-demand) | PAY_PER_REQUEST billing: no provisioned capacity, no idle floor; only stored-item storage | Near-zero (storage only) |
| S3 Vectors (bucket + index) | Storage of persisted vectors only | Near-zero (storage only) |
| Lambda (pipeline + serving) | Scale-to-zero, no provisioned concurrency (asserted by the serving-stack synth test `test_scale_to_zero_no_provisioned_concurrency`) | Zero when idle |
| HTTP API Gateway | Per-request; no idle charge | Zero when idle |
| Bedrock Guardrail | No idle floor; charged per policy evaluation at call time | Zero when idle |
| Bedrock Knowledge Base (S3 Vectors backed) | No idle floor; no OpenSearch Serverless collection is provisioned by default (that would carry a standing floor) | Zero when idle |
| Step Functions | Per state transition; no idle charge | Zero when idle |

Net idle cost is near-zero: on-demand DynamoDB, S3 Vectors storage, scale-to-zero Lambda,
and a per-request HTTP API have no standing compute floor.

### Active cost (enrichment runs and demo queries)

Cost accrues per unit of work. Rough drivers:

| Activity | Cost driver | Notes |
| --- | --- | --- |
| Enrichment Converse call | Bedrock Sonnet 4.5 input + output tokens, once per movie (plus repair retries) | The validate/repair loop is bounded, so retries per movie are capped. |
| `/query` agent turn | Bedrock Sonnet 4.5 input + output tokens per turn | The agent loop is bounded by `MAX_TURNS`, capping Converse calls per request. |
| Overview embedding | Amazon Titan Text Embeddings V2, once per non-blank movie overview | Blank/whitespace overviews are skipped (~2.1%, see the embed guard), so they incur no Titan call. |
| Guardrail evaluation | Per Converse call, both subsystems | One Guardrail evaluation per input and per output. |
| Step Functions transitions | Per state transition per movie | Extract -> per-item chain (ComputePES, Enrich, Validate, Persist, Embed) -> Evaluate. |
| DynamoDB read/write | On-demand request units | Writes during enrichment persist; reads during `/query`. |

The dominant active cost is Bedrock Converse (Sonnet 4.5) tokens, scaled by the number of
movies enriched and the number of query turns served. Titan embeddings and Step Functions
transitions are a small per-item cost.

## Teardown verification checklist

Destroy in reverse dependency order (reverse of create). The serving stack depends on the
deployed KB and pipeline resources, and the KB depends on the pipeline's S3 Vectors store,
so tear down serving first, then the KB, then the pipeline.

Destroy order:

1. `MovieIntelServingStack`
2. `KnowledgeBaseStack`
3. `MovieIntelPipelineStack`

### Resources that may not auto-delete

- S3 Vectors bucket contents: vectors persist independently of the index/bucket resource.
  The index and bucket (and their contents) may need to be emptied and deleted manually
  before or after the pipeline-stack destroy completes.
- CloudWatch log groups: per-Lambda `/aws/lambda/...` log groups and the Step Functions
  state-machine log group can linger after stack deletion.
- CDK bootstrap stack (`CDKToolkit`): leave it alone. It is shared infrastructure, not part
  of this application.
- DynamoDB table: the `MovieIntel` table is created with `RemovalPolicy.DESTROY`, so it is
  deleted with the pipeline stack. Confirm it is gone (see commands below).

### Non-destructive verification commands

Run these AFTER destroy to confirm nothing lingers. These are read-only; they do not delete
anything.

Confirm the three stacks are gone (each should return no stack / a `does not exist` error):

```
aws cloudformation describe-stacks --stack-name MovieIntelServingStack
aws cloudformation describe-stacks --stack-name KnowledgeBaseStack
aws cloudformation describe-stacks --stack-name MovieIntelPipelineStack
```

Confirm the S3 Vectors index/bucket are gone (or empty, so no vectors linger):

```
aws s3vectors list-indexes --vector-bucket-name movieintel-vectors
aws s3vectors list-vectors --vector-bucket-name movieintel-vectors --index-name movieintel-overviews
```

Confirm no MovieIntel Lambda log groups remain:

```
aws logs describe-log-groups --log-group-name-prefix /aws/lambda/MovieIntel
```

Confirm the DynamoDB table is gone:

```
aws dynamodb describe-table --table-name MovieIntel
```

> Note: this task does NOT run `cdk destroy`. The commands above are for verification after
> a deliberate, separate teardown.
