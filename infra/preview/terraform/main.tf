variable "ssh_public_key" { type = string }
variable "ssh_private_key_path" { type = string }
variable "ingress_host" { default = "129.241.100.225" }
variable "ingress_server_id" { default = "638e1c4a-4d33-4f64-a3f3-f28061d8cacf" }
variable "ingress_private_key_path" { type = string }
variable "external_network_id" { default = "9acc59cd-6b68-4075-8b28-5abeaa1379c5" }
variable "image_id" { default = "9e481427-d702-460b-a9fd-c3053f216518" }
variable "flavor_id" { default = "aef91d31-ffa8-4b14-9c14-a38ebb757358" }
variable "secrets_directory" {
  description = "Private off-server recovery copy; later replace the config source with Doppler."
  type        = string
}
resource "openstack_networking_network_v2" "preview" { name = "monoweb-preview-poc" }
resource "openstack_networking_subnet_v2" "preview" {
  name            = "monoweb-preview-poc"
  network_id      = openstack_networking_network_v2.preview.id
  cidr            = "192.168.73.0/24"
  ip_version      = 4
  dns_nameservers = ["1.1.1.1", "8.8.8.8"]
}
resource "openstack_networking_router_v2" "preview" {
  name                = "monoweb-preview-poc"
  external_network_id = var.external_network_id
}
resource "openstack_networking_router_interface_v2" "preview" {
  router_id = openstack_networking_router_v2.preview.id
  subnet_id = openstack_networking_subnet_v2.preview.id
}
resource "openstack_networking_secgroup_v2" "preview" {
  name        = "monoweb-preview-poc"
  description = "Monoweb previews: HTTPS/HTTP and SSH"
}
resource "openstack_networking_secgroup_rule_v2" "preview" {
  for_each          = { ssh = 22, http = 80, https = 443 }
  security_group_id = openstack_networking_secgroup_v2.preview.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = each.value
  port_range_max    = each.value
  remote_ip_prefix  = each.key == "ssh" ? "192.168.73.254/32" : "0.0.0.0/0"
}
resource "openstack_compute_keypair_v2" "preview" {
  name       = "monoweb-preview-poc"
  public_key = var.ssh_public_key
}
resource "openstack_compute_instance_v2" "preview" {
  name            = "monoweb-preview-poc"
  flavor_id       = var.flavor_id
  key_pair        = openstack_compute_keypair_v2.preview.name
  security_groups = [openstack_networking_secgroup_v2.preview.name]
  block_device {
    uuid                  = var.image_id
    source_type           = "image"
    destination_type      = "volume"
    volume_size           = 80
    boot_index            = 0
    delete_on_termination = true
  }
  network {
    uuid        = openstack_networking_network_v2.preview.id
    fixed_ip_v4 = "192.168.73.66"
  }
  user_data = "#cloud-config\npackage_update: true\npackages: [curl, git, python3, jq]\n"
  lifecycle { ignore_changes = [user_data] }
  depends_on = [openstack_networking_router_interface_v2.preview]
}
resource "openstack_networking_secgroup_v2" "ingress" {
  name        = "monoweb-preview-ingress"
  description = "HTTP and HTTPS forwarding for Monoweb previews"
}
resource "openstack_networking_secgroup_rule_v2" "ingress" {
  for_each          = { http = 80, https = 443 }
  security_group_id = openstack_networking_secgroup_v2.ingress.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = each.value
  port_range_max    = each.value
  remote_ip_prefix  = "0.0.0.0/0"
}
resource "openstack_networking_port_v2" "ingress" {
  name               = "monoweb-preview-court-link"
  network_id         = openstack_networking_network_v2.preview.id
  security_group_ids = ["8ba4c1ed-135a-4f59-87d6-d2ac5f8438d9", openstack_networking_secgroup_v2.ingress.id]
  fixed_ip {
    subnet_id  = openstack_networking_subnet_v2.preview.id
    ip_address = "192.168.73.254"
  }
}
resource "openstack_compute_interface_attach_v2" "ingress" {
  instance_id = var.ingress_server_id
  port_id     = openstack_networking_port_v2.ingress.id
}
# Existing Minecraft public NIC is a reference; only this group association is owned here.
variable "ingress_public_port_id" { type = string }
resource "openstack_networking_port_secgroup_associate_v2" "ingress" {
  port_id            = var.ingress_public_port_id
  security_group_ids = [openstack_networking_secgroup_v2.ingress.id]
  enforce            = false
}
output "preview_private_ip" { value = "192.168.73.66" }
# Independent of the boot disk: retain Coolify's database, keys, and preview credentials on VM loss.
resource "openstack_blockstorage_volume_v3" "data" {
  name = "monoweb-preview-data"
  size = 40
  lifecycle { prevent_destroy = true }
}
resource "openstack_compute_volume_attach_v2" "data" {
  instance_id = openstack_compute_instance_v2.preview.id
  volume_id   = openstack_blockstorage_volume_v3.data.id
}
