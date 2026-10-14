resource "terraform_data" "bootstrap" {
  triggers_replace = {
    server = openstack_compute_instance_v2.preview.id
    volume = openstack_compute_volume_attach_v2.data.id
    code   = sha256(join("", [for file in ["bootstrap.py", "previewctl.py", "build.sh", "compose.yaml", "reconcile.service", "reconcile.timer"] : filesha256("${path.module}/../${file}")]))
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
    inline = ["mkdir -p /tmp/monoweb-preview-bootstrap/recovery", "chmod 700 /tmp/monoweb-preview-bootstrap /tmp/monoweb-preview-bootstrap/recovery"]
  }
  provisioner "file" {
    source      = "${var.secrets_directory}/"
    destination = "/tmp/monoweb-preview-bootstrap/recovery"
  }
  provisioner "file" {
    source      = "${path.module}/../bootstrap.py"
    destination = "/tmp/monoweb-preview-bootstrap/bootstrap.py"
  }
  provisioner "file" {
    source      = "${path.module}/../previewctl.py"
    destination = "/tmp/monoweb-preview-bootstrap/previewctl.py"
  }
  provisioner "file" {
    source      = "${path.module}/../build.sh"
    destination = "/tmp/monoweb-preview-bootstrap/build.sh"
  }
  provisioner "file" {
    source      = "${path.module}/../compose.yaml"
    destination = "/tmp/monoweb-preview-bootstrap/compose.yaml"
  }
  provisioner "file" {
    source      = "${path.module}/../reconcile.service"
    destination = "/tmp/monoweb-preview-bootstrap/reconcile.service"
  }
  provisioner "file" {
    source      = "${path.module}/../reconcile.timer"
    destination = "/tmp/monoweb-preview-bootstrap/reconcile.timer"
  }
  provisioner "remote-exec" {
    inline = ["sudo python3 /tmp/monoweb-preview-bootstrap/bootstrap.py /tmp/monoweb-preview-bootstrap ${openstack_blockstorage_volume_v3.data.id}"]
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
  provisioner "file" {
    content = yamlencode({ network = { version = 2, ethernets = { ens7 = {
      match = { macaddress = openstack_networking_port_v2.ingress.mac_address }
      dhcp4 = false, dhcp6 = false, addresses = ["192.168.73.254/24"], optional = true
    } } } })
    destination = "/tmp/monoweb-preview-netplan.yaml"
  }
  provisioner "file" {
    source      = "${path.module}/../ingress/"
    destination = "/tmp/monoweb-preview-ingress"
  }
  provisioner "remote-exec" {
    inline = [
      "sudo install -m 600 /tmp/monoweb-preview-netplan.yaml /etc/netplan/90-monoweb-preview.yaml",
      "sudo install -m 644 /tmp/monoweb-preview-ingress/* /etc/systemd/system/",
      "sudo netplan generate && sudo networkctl reload && sudo networkctl reconfigure ens7",
      "sudo systemctl daemon-reload && sudo systemctl enable --now monoweb-preview-http.socket monoweb-preview-https.socket"
    ]
  }
}
