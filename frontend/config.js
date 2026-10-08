// Runtime configuration for the static front end.
//
// window.API_BASE_URL is the base URL of the serving HTTP API. It is set by the
// deployer from the MovieIntelServingStack `ServingApiUrl` CfnOutput, and this
// file is the ONLY file edited between the two deploy steps (design 5.2): the
// front end is deployed once with the placeholder, the serving API URL is read
// from stack outputs, this value is replaced, then the front end is redeployed.
window.API_BASE_URL = "https://REPLACE_ME.execute-api.us-east-1.amazonaws.com";
