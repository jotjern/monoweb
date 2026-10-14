terraform {
  required_version = ">= 1.6"
  backend "local" {}
  required_providers {
    coolify = {
      source  = "coolify-terraform/coolify"
      version = "0.1.26"
    }
  }
}
provider "coolify" {}
variable "project_uuid" { type = string }
variable "server_uuid" { type = string }
variable "environment_json_file" { type = string }
variable "webhook_secret" {
  type      = string
  sensitive = true
}
variable "base_domain" {
  default = "129.241.100.225.sslip.io"
  type    = string
}

resource "coolify_application" "monoweb" {
  name                    = "Monoweb previews"
  project_uuid            = var.project_uuid
  server_uuid             = var.server_uuid
  environment_name        = "preview"
  git_repository          = "https://github.com/jotjern/monoweb"
  git_commit_sha          = "HEAD"
  git_branch              = "main"
  build_pack              = "dockercompose"
  ports_exposes           = "3000"
  docker_compose_location = "/infra/preview/compose.yaml"
  docker_compose_domains = jsonencode([
    { name = "web", domain = "https://main.${var.base_domain}:3000" },
    { name = "rpc", domain = "https://main-api.${var.base_domain}:4444" }
  ])
  docker_compose_custom_build_command = "sh -c 'docker run --rm -v /opt/monoweb-previews/hooks:/hooks:ro coollabsio/coolify-helper:1.0.17 cat /hooks/build.sh > /tmp/preview-build.sh && sh /tmp/preview-build.sh'"
  is_pr_deployments_public_enabled    = false
  is_auto_deploy_enabled              = true
  is_preview_deployments_enabled      = true
  use_build_secrets                   = true
  manual_webhook_secret_github        = var.webhook_secret
  instant_deploy                      = false
}

# Standard JSON export now; this source can become a Doppler export or vault references later.
locals {
  environment = sensitive(jsondecode(file(var.environment_json_file)))
  variables   = { for pair in setproduct(nonsensitive(keys(local.environment)), ["main", "preview"]) : "${pair[1]}:${pair[0]}" => { key = pair[0], preview = pair[1] == "preview" } }
}
resource "coolify_environment_variable" "configuration" {
  for_each         = local.variables
  application_uuid = coolify_application.monoweb.uuid
  key              = each.value.key
  value            = local.environment[each.value.key]
  is_preview       = each.value.preview
  is_literal       = true
  is_build         = each.value.key != "NODE_OPTIONS"
  is_runtime       = !contains(["PREVIEW_ADMIN_ID", "PREVIEW_ADMIN_EMAIL"], each.value.key)
}
output "application_uuid" { value = coolify_application.monoweb.uuid }
