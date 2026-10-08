"""Synth/assertion tests for the front-end stack (FEAT-003, design 4.2/5.2).

These synthesize ``MovieIntelFrontendStack`` into a CloudFormation template with
``aws_cdk.assertions.Template`` and assert the contract the stack must satisfy:

* exactly one private S3 bucket with all four ``PublicAccessBlockConfiguration`` flags true;
* a CloudFront distribution whose ``DefaultRootObject`` is ``index.html`` and whose default
  behavior redirects viewers to HTTPS;
* an Origin Access Control resource (OAC, not a public bucket, not OAI);
* the S3 bucket policy grants read to the CloudFront service principal, never public ``*``;
* a ``Custom::CDKBucketDeployment`` that uploads ``frontend/`` to the bucket;
* a ``ResponseHeadersPolicy`` whose CSP string allows the ``execute-api`` origin;
* a non-empty set of stack outputs (the CloudFront URL);
* no em dash (``\u2014``) in any asserted name/description/comment (AWS steering: hyphens).
"""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from frontend_stack import MovieIntelFrontendStack


@pytest.fixture(scope="module")
def template() -> Template:
    """Synthesize the stack once per module."""
    app = cdk.App()
    stack = MovieIntelFrontendStack(
        app, "MovieIntelFrontendStack", env=cdk.Environment(region="us-east-1")
    )
    return Template.from_stack(stack)


def test_single_private_bucket_blocks_all_public_access(template: Template) -> None:
    """Exactly one S3 bucket with all four public-access-block flags set true."""
    template.resource_count_is("AWS::S3::Bucket", 1)
    buckets = template.find_resources("AWS::S3::Bucket")
    (props,) = (res["Properties"] for res in buckets.values())
    block = props.get("PublicAccessBlockConfiguration")
    assert isinstance(block, dict), props
    assert block.get("BlockPublicAcls") is True, block
    assert block.get("BlockPublicPolicy") is True, block
    assert block.get("IgnorePublicAcls") is True, block
    assert block.get("RestrictPublicBuckets") is True, block


def test_cloudfront_distribution_root_object_and_https(template: Template) -> None:
    """The distribution serves index.html by default and redirects viewers to HTTPS."""
    template.resource_count_is("AWS::CloudFront::Distribution", 1)
    distributions = template.find_resources("AWS::CloudFront::Distribution")
    (props,) = (res["Properties"] for res in distributions.values())
    config = props["DistributionConfig"]
    assert config["DefaultRootObject"] == "index.html", config
    assert config["DefaultCacheBehavior"]["ViewerProtocolPolicy"] == "redirect-to-https", config


def test_origin_access_control_exists(template: Template) -> None:
    """An Origin Access Control resource exists (OAC, not a public bucket, not OAI)."""
    template.resource_count_is("AWS::CloudFront::OriginAccessControl", 1)


def test_bucket_policy_grants_cloudfront_service_not_public(template: Template) -> None:
    """The bucket policy ALLOW principal is the CloudFront service, never public ``*``.

    The only public ``AWS: *`` principal permitted is on the ``Deny`` SSL-only statement
    that ``enforce_ssl=True`` adds; no ``Allow`` statement may grant a public principal.
    The object-read grant must name the CloudFront service principal (OAC).
    """
    policies = template.find_resources("AWS::S3::BucketPolicy")
    assert policies, "no S3 bucket policy found"
    saw_cloudfront_principal = False
    for res in policies.values():
        for stmt in res["Properties"]["PolicyDocument"]["Statement"]:
            principal = stmt.get("Principal")
            principal_text = json.dumps(principal)
            if stmt.get("Effect") == "Allow":
                assert principal != "*", "bucket policy grants a public '*' Allow principal"
                assert '"AWS": "*"' not in principal_text, (
                    "bucket policy grants a public AWS '*' Allow principal"
                )
            if "cloudfront.amazonaws.com" in principal_text:
                saw_cloudfront_principal = True
    assert saw_cloudfront_principal, "bucket policy does not grant the CloudFront service principal"


def test_bucket_deployment_resource_present(template: Template) -> None:
    """A Custom::CDKBucketDeployment uploads frontend/ to the bucket."""
    template.resource_count_is("Custom::CDKBucketDeployment", 1)


def test_response_headers_policy_csp_allows_execute_api(template: Template) -> None:
    """A ResponseHeadersPolicy carries a CSP whose connect-src allows execute-api.

    The policy name/comment and CSP string must use hyphens only, never em dashes.
    """
    template.resource_count_is("AWS::CloudFront::ResponseHeadersPolicy", 1)
    policies = template.find_resources("AWS::CloudFront::ResponseHeadersPolicy")
    (props,) = (res["Properties"] for res in policies.values())
    config = props["ResponseHeadersPolicyConfig"]
    security = config["SecurityHeadersConfig"]
    csp = security["ContentSecurityPolicy"]["ContentSecurityPolicy"]
    assert "execute-api" in csp, csp
    assert "\u2014" not in csp, "em dash in CSP string"
    assert "\u2014" not in json.dumps(config), "em dash in response headers policy config"
    # HSTS and a DENY frame policy are also present.
    assert "StrictTransportSecurity" in security, security
    assert security["FrameOptions"]["FrameOption"] == "DENY", security


def test_stack_exports_the_frontend_url(template: Template) -> None:
    """The CloudFront URL is exported as a CfnOutput."""
    outputs = template.find_outputs("*")
    assert outputs, "no stack outputs found"


def test_no_em_dash_in_names_or_descriptions(template: Template) -> None:
    """No asserted name/description/comment contains an em dash (AWS steering: hyphens)."""
    distributions = template.find_resources("AWS::CloudFront::Distribution")
    for res in distributions.values():
        comment = res["Properties"]["DistributionConfig"].get("Comment", "")
        assert "\u2014" not in comment, "em dash in distribution comment"

    outputs = template.find_outputs("*")
    for output in outputs.values():
        assert "\u2014" not in output.get("Description", ""), "em dash in output description"
