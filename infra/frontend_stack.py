"""CDK stack serving the movieintel static front end from CloudFront + a private S3 bucket.

This stack provisions the front-end delivery layer (FEAT-003, design 4.2/5.2): a private
S3 bucket with ALL public access blocked, fronted by a CloudFront distribution that reads
the bucket through Origin Access Control (OAC, not the legacy OAI and not a public bucket
policy). The site assets in ``frontend/`` are packaged at synth time via a
``BucketDeployment`` that uploads them to the bucket and invalidates ``/*`` on the
distribution so a redeploy serves fresh files. A ``ResponseHeadersPolicy`` attaches HSTS,
``X-Content-Type-Options``, a ``DENY`` frame policy, and a Content-Security-Policy whose
``connect-src`` allows the ``execute-api`` origin the browser calls.

Two-step deploy procedure (design 5.2), because the serving CORS origin and the front
end's API base URL are mutually dependent:

1. Deploy ``MovieIntelServingStack`` and read its ``ServingApiUrl`` output.
2. Set ``frontend/config.js`` ``API_BASE_URL`` to that ``ServingApiUrl``.
3. Deploy this stack (``MovieIntelFrontendStack``) and read its ``FrontendUrl`` output.
4. Pin the serving CORS origin to the CloudFront domain by redeploying the serving stack
   with ``--context cors_allow_origin=https://<FrontendUrl-domain>``.

AWS steering: hyphens, never em dashes, in names and descriptions.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from constants import REPO_ROOT
from constructs import Construct


class MovieIntelFrontendStack(Stack):
    """Private S3 + CloudFront (OAC) delivery of the movieintel static front end."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)  # type: ignore[arg-type]

        self.bucket = self._site_bucket()
        self.distribution = self._distribution()
        self._deploy_site()
        self._outputs()

    # -- bucket ---------------------------------------------------------------

    def _site_bucket(self) -> s3.Bucket:
        """Private origin bucket: all public access blocked, SSE-S3, SSL enforced.

        No ``bucket_name`` (let CloudFormation name it) and no public bucket policy - the
        only read principal is CloudFront via the OAC wired in :meth:`_distribution`. The
        bucket is destroyed with the stack and its objects auto-deleted so a teardown is
        clean.
        """
        return s3.Bucket(
            self,
            "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

    # -- response headers -----------------------------------------------------

    def _response_headers_policy(self) -> cloudfront.ResponseHeadersPolicy:
        """Security headers for every viewer response (HSTS, nosniff, frame-deny, CSP).

        The CSP ``connect-src`` currently allows any ``execute-api`` host in us-east-1 so
        the front end can call the serving HTTP API before its concrete id is known.
        """
        return cloudfront.ResponseHeadersPolicy(
            self,
            "SecurityHeadersPolicy",
            comment="movieintel front-end security headers - HSTS, nosniff, frame-deny, CSP.",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365),
                    include_subdomains=True,
                    preload=True,
                    override=True,
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(
                    override=True,
                ),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY,
                    override=True,
                ),
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    # TODO(csp): pin connect-src to the concrete
                    # https://<apiId>.execute-api.us-east-1.amazonaws.com once the serving
                    # stack is deployed (design 4.2); the wildcard host is a first-deploy
                    # convenience while the api id is unknown.
                    content_security_policy=(
                        "default-src 'self'; "
                        "style-src 'self' 'unsafe-inline'; "
                        "script-src 'self'; "
                        "connect-src 'self' https://*.execute-api.us-east-1.amazonaws.com"
                    ),
                    override=True,
                ),
            ),
        )

    # -- distribution ---------------------------------------------------------

    def _distribution(self) -> cloudfront.Distribution:
        """CloudFront distribution reading the private bucket via Origin Access Control.

        ``S3BucketOrigin.with_origin_access_control`` wires the CloudFront-only read bucket
        policy automatically (no public bucket, no OAI). Viewers are redirected to HTTPS and
        the security headers policy is attached to the default behavior.
        """
        origin = origins.S3BucketOrigin.with_origin_access_control(self.bucket)
        return cloudfront.Distribution(
            self,
            "SiteDistribution",
            comment="movieintel front-end distribution - private S3 origin via OAC.",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                response_headers_policy=self._response_headers_policy(),
            ),
        )

    # -- deployment -----------------------------------------------------------

    def _deploy_site(self) -> None:
        """Upload ``frontend/`` to the bucket and invalidate ``/*`` on the distribution.

        The source is packaged at synth time from ``REPO_ROOT / 'frontend'``; passing the
        distribution with ``distribution_paths=['/*']`` invalidates the whole cache so a
        redeploy serves the new assets.
        """
        s3deploy.BucketDeployment(
            self,
            "SiteDeployment",
            sources=[s3deploy.Source.asset(str(REPO_ROOT / "frontend"))],
            destination_bucket=self.bucket,
            distribution=self.distribution,
            distribution_paths=["/*"],
        )

    # -- outputs --------------------------------------------------------------

    def _outputs(self) -> None:
        """Export the CloudFront URL (the origin to pin as the serving CORS allow-origin)."""
        CfnOutput(
            self,
            "FrontendUrl",
            value=f"https://{self.distribution.distribution_domain_name}",
            description="CloudFront URL of the front end - pin as the serving CORS origin.",
        )
