resource "terraform_data" "bootstrap" {
  triggers_replace = {
    server = openstack_compute_instance_v2.preview.id
    volume = openstack_compute_volume_attach_v2.data.id
    code   = sha256(join("", [for name in ["bootstrap.sh", "build.sh", "database.sh", "runtime.mjs", "coolify-compose.custom.yml"] : filesha256("${path.module}/../${name}")]))
  }
  connection {
    type                = "ssh"
    host                = "192.168.73.66"
    user                = "ubuntu"
    private_key         = file(var.ssh_private_key_path)
    bastion_host        = var.ingress_host
    bastion_user        = "ubuntu"
    bastion_private_key = file(var.ingress_private_key_path)
    timeout             = "10m"
  }
  provisioner "remote-exec" {
    inline = ["set -eu", "mkdir -p /tmp/monoweb-preview-bootstrap/recovery", "chmod 700 /tmp/monoweb-preview-bootstrap /tmp/monoweb-preview-bootstrap/recovery"]
  }
  provisioner "file" {
    source      = "${var.secrets_directory}/"
    destination = "/tmp/monoweb-preview-bootstrap/recovery"
  }
  provisioner "file" {
    source      = "${path.module}/../bootstrap.sh"
    destination = "/tmp/monoweb-preview-bootstrap/bootstrap.sh"
  }
  provisioner "file" {
    source      = "${path.module}/../build.sh"
    destination = "/tmp/monoweb-preview-bootstrap/build.sh"
  }
  provisioner "file" {
    source      = "${path.module}/../database.sh"
    destination = "/tmp/monoweb-preview-bootstrap/database.sh"
  }
  provisioner "file" {
    source      = "${path.module}/../runtime.mjs"
    destination = "/tmp/monoweb-preview-bootstrap/runtime.mjs"
  }
  provisioner "file" {
    source      = "${path.module}/../coolify-compose.custom.yml"
    destination = "/tmp/monoweb-preview-bootstrap/coolify-compose.custom.yml"
  }
  provisioner "remote-exec" {
    inline = ["set -eu", "sudo bash /tmp/monoweb-preview-bootstrap/bootstrap.sh /tmp/monoweb-preview-bootstrap ${openstack_blockstorage_volume_v3.data.id}"]
  }
}

resource "terraform_data" "ingress" {
  triggers_replace = {
    port   = openstack_compute_interface_attach_v2.ingress.id
    config = sha256(join("", [for name in ["http.service", "http.socket", "https.service", "https.socket"] : filesha256("${path.module}/../ingress/monoweb-preview-${name}")]))
  }
  connection {
    type        = "ssh"
    host        = var.ingress_host
    user        = "ubuntu"
    private_key = file(var.ingress_private_key_path)
  }
  provisioner "remote-exec" {
    inline = ["set -eu", "mkdir -p /tmp/monoweb-coolify-ingress"]
  }
  provisioner "file" {
    content = yamlencode({ network = { version = 2, ethernets = { ens7 = {
      match = { macaddress = openstack_networking_port_v2.ingress.mac_address }
      dhcp4 = false, dhcp6 = false, addresses = ["192.168.73.254/24"], optional = true
    } } } })
    destination = "/tmp/monoweb-preview-netplan.yaml"
  }
  provisioner "file" {
    source      = "${path.module}/../ingress/"
    destination = "/tmp/monoweb-coolify-ingress"
  }
  provisioner "remote-exec" {
    inline = [
      "set -eu",
      "sudo install -m 600 /tmp/monoweb-preview-netplan.yaml /etc/netplan/90-monoweb-preview.yaml",
      "sudo install -m 644 /tmp/monoweb-coolify-ingress/* /etc/systemd/system/",
      "sudo netplan generate && sudo networkctl reload && sudo networkctl reconfigure ens7",
      "sudo systemctl daemon-reload && sudo systemctl enable --now monoweb-preview-http.socket monoweb-preview-https.socket"
    ]
  }
}
