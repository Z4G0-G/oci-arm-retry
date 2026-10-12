import oci
import time
import datetime

# ╔══════════════════════════════════════════════════════╗
# ║  Target : VM.Standard.A1.Flex (ARM)                  ║
# ║  Spec   : 2 OCPU / 12 GB RAM / 100 GB disk (default) ║
# ║  Arch   : ARM (Ampere)                               ║
# ║  Tier   : Oracle Always Free                         ║
# ║  OS     : Canonical Ubuntu 22.04                     ║
# ║         : Canonical Ubuntu 22.04 Minimal aarch64     ║
# ║         : Canonical Ubuntu 24.04 Minimal aarch64     ║
# ╚══════════════════════════════════════════════════════╝

# ─── Configuration ───────────────────────────────────────
COMPARTMENT_ID = (
    "ocid1.tenancy.oc1..aaaaaaaa2h32kfvrzmuigjnuyob6qmyvzttrrhjbop6cxsy3adu45szyhoka"  # Replace with your tenancy OCID
)
SSH_PUBLIC_KEY = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDIkOcUxgYIgLl4zrw9LAG+V19LXobq9GlePQVZYg8op4s25aisFt4dod+Z/WW7z3LAVGAM81GYSKuiCaCGlLaLaRTeTz4p4CrC5lk5RW+yLyx7mESmpFjAnVaPMkXCEcm9D7DIVCgDC7AVLeIqdgRz0CtxSW2li4mMYdVYpBO8Zm3CcreDE6qU6fe4uAQi2TcywPYn2muhcc/8clDLBl/iSM2YRcQSkWFHU0sjienBDNUzokJPnLcshwbnGtossIS7vQcVoi9fUzRebnxxxjfJNtJPOh0yztdmmsj9RLkR3FJOEMMJJioHYIGrbFHdqQoSFyY/JIQFLygxL5Jp1umh ssh-key-2026-10-12"  # Replace with your SSH public key (.pub file content)
INSTANCE_NAME = "zago-arm-server"
ARM_OCPUS = 2
ARM_MEMORY_IN_GBS = 12
BOOT_VOLUME_SIZE_IN_GBS = 200
RETRY_INTERVAL = 60  # seconds
# ────────────────────────────────────────────────────────

# ─── OCI Authentication ──────────────────────────────────
# [Local] Create a config file at ~/.oci/config
#   See README.md for format; key_file should point to your API private key .pem
#
# [GitHub Actions] No config file needed
#   The workflow auto-creates it from GitHub Secrets — see README.md
# ────────────────────────────────────────────────────────
config = oci.config.from_file()


def get_all_availability_domains():
    """返回该区域内所有可用域的名称列表"""
    identity = oci.identity.IdentityClient(config)
    ads = identity.list_availability_domains(COMPARTMENT_ID).data
    return [ad.name for ad in ads]


########################################################
#
# 抢 X86版本的 Canonical Ubuntu 22.04 
#
#######################################################

# def get_ubuntu_arm_image():
#     compute = oci.core.ComputeClient(config)
#     images = compute.list_images(
#         COMPARTMENT_ID,
#         operating_system="Canonical Ubuntu",
#         operating_system_version="22.04",
#         shape="VM.Standard.A1.Flex",
#         sort_by="TIMECREATED",
#         sort_order="DESC",
#     ).data
#     if not images:
#         raise Exception("Ubuntu 22.04 ARM image not found")
#     return images[0].id

########################################################
#
# 抢 ARM版本的 Canonical Ubuntu 24.04 Minimal aarch64 
#
#######################################################
def get_ubuntu_arm_image():
    """优先 24.04 Minimal aarch64，其次 22.04 Minimal aarch64"""
    compute = oci.core.ComputeClient(config)
    images = compute.list_images(
        COMPARTMENT_ID,
        operating_system="Canonical Ubuntu",
        shape="VM.Standard.A1.Flex",
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data

    for version in ["24.04", "22.04"]:
        for img in images:
            name = (img.display_name or "").lower()
            if version in name and "minimal" in name and "aarch64" in name:
                print(f"Selected image: {img.display_name}")
                return img.id
    raise Exception("Ubuntu 24.04/22.04 Minimal aarch64 ARM image not found")


def ensure_vcn(network):
    """确保 VCN、互联网网关、路由表、安全列表已存在"""
    vcns = network.list_vcns(COMPARTMENT_ID, display_name="retry-vcn").data
    if vcns:
        vcn = vcns[0]
        print(f"Using existing VCN: {vcn.id}")
        return vcn

    vcn = network.create_vcn(
        oci.core.models.CreateVcnDetails(
            compartment_id=COMPARTMENT_ID,
            display_name="retry-vcn",
            cidr_block="10.0.0.0/16",
        )
    ).data
    print(f"Created VCN: {vcn.id}")

    # 互联网网关
    ig = network.create_internet_gateway(
        oci.core.models.CreateInternetGatewayDetails(
            compartment_id=COMPARTMENT_ID,
            vcn_id=vcn.id,
            display_name="retry-ig",
            is_enabled=True,
        )
    ).data

    # 默认路由表指向互联网网关
    network.update_route_table(
        vcn.default_route_table_id,
        oci.core.models.UpdateRouteTableDetails(
            route_rules=[
                oci.core.models.RouteRule(
                    destination="0.0.0.0/0",
                    network_entity_id=ig.id,
                )
            ]
        ),
    )

    # 打开入站端口：SSH / HTTP / HTTPS / Streamlit
    security_lists = network.list_security_lists(COMPARTMENT_ID, vcn_id=vcn.id).data
    if security_lists:
        existing_egress = security_lists[0].egress_security_rules
        new_ingress = []
        for port in [22, 80, 443, 8501]:
            new_ingress.append(
                oci.core.models.IngressSecurityRule(
                    protocol="6",
                    source="0.0.0.0/0",
                    tcp_options=oci.core.models.TcpOptions(
                        destination_port_range=oci.core.models.PortRange(
                            min=port, max=port
                        )
                    ),
                )
            )
        network.update_security_list(
            security_lists[0].id,
            oci.core.models.UpdateSecurityListDetails(
                ingress_security_rules=new_ingress,
                egress_security_rules=existing_egress,
            ),
        )

    return vcn


def ensure_subnet(network, vcn):
    """创建或复用区域性子网（可跨 AD 使用）"""
    subnets = network.list_subnets(
        COMPARTMENT_ID, vcn_id=vcn.id, display_name="retry-subnet"
    ).data
    if subnets:
        print(f"Using existing subnet: {subnets[0].id}")
        return subnets[0].id

    subnet = network.create_subnet(
        oci.core.models.CreateSubnetDetails(
            compartment_id=COMPARTMENT_ID,
            vcn_id=vcn.id,
            display_name="retry-subnet",
            cidr_block="10.0.0.0/24",
            prohibit_public_ip_on_vnic=False,
        )
    ).data
    print(f"Created subnet: {subnet.id}")
    return subnet.id


def try_create_instance(subnet_id, ad_name, image_id):
    compute = oci.core.ComputeClient(config)
    instance = compute.launch_instance(
        oci.core.models.LaunchInstanceDetails(
            compartment_id=COMPARTMENT_ID,
            display_name=INSTANCE_NAME,
            availability_domain=ad_name,
            shape="VM.Standard.A1.Flex",
            shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=ARM_OCPUS,
                memory_in_gbs=ARM_MEMORY_IN_GBS,
            ),
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                image_id=image_id,
                boot_volume_size_in_gbs=BOOT_VOLUME_SIZE_IN_GBS,
            ),
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=subnet_id,
                assign_public_ip=True,
            ),
            metadata={"ssh_authorized_keys": SSH_PUBLIC_KEY},
        )
    ).data
    return instance


def main():
    print("Initializing network configuration...")
    network = oci.core.VirtualNetworkClient(config)
    vcn = ensure_vcn(network)
    subnet_id = ensure_subnet(network, vcn)

    print("Fetching all availability domains...")
    ad_names = get_all_availability_domains()
    print(f"Available ADs: {ad_names}")

    print("Fetching Ubuntu ARM image...")
    image_id = get_ubuntu_arm_image()
    print(f"Image ID: {image_id}")

    attempt = 0
    while True:
        attempt += 1
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{now}] === Round #{attempt} ===")

        for ad in ad_names:
            try:
                instance = try_create_instance(subnet_id, ad, image_id)
                print(f"\n✅ Success! Instance created in {ad}")
                print(f"   ID: {instance.id}")
                print(f"   State: {instance.lifecycle_state}")
                print(f"   Check Oracle Cloud Console for the public IP")
                return
            except oci.exceptions.ServiceError as e:
                msg = str(e).lower()
                if "capacity" in msg:
                    print(f"❌ {ad}: Out of capacity")
                else:
                    print(f"❌ {ad}: API error: {e.message}")
            except Exception as e:
                print(f"⚠️ {ad}: {type(e).__name__}: {e}")

        print(f"All ADs exhausted. Sleeping {RETRY_INTERVAL}s...")
        time.sleep(RETRY_INTERVAL)


if __name__ == "__main__":
    main()
