"""Intenta crear la instancia talurio-srv en Oracle Cloud hasta conseguir capacidad.

Lo ejecuta GitHub Actions cada pocos minutos. Cada ejecución hace un intento:
- Si la instancia ya existe, avisa y termina.
- Si Oracle no tiene capacidad (o frena por exceso de pedidos), termina sin error
  para volver a intentar en la próxima ejecución.
- Si la crea, escribe creada=true para que el workflow se apague y avise.
"""
import os
import sys

import oci

NOMBRE = "talurio-srv"
FORMA = "VM.Standard.A1.Flex"
OCPUS = float(os.getenv("OCPUS", "1"))
MEMORIA_GB = float(os.getenv("MEMORIA_GB", "6"))
DISCO_GB = int(os.getenv("DISCO_GB", "100"))
CLAVE_SSH = os.environ["CLAVE_SSH"].strip()


def aviso(nivel, texto):
    """Muestra el resultado en la pantalla de resumen de GitHub (Annotations)."""
    print(f"::{nivel} title=Resultado::{texto}")


def salida(clave, valor):
    ruta = os.getenv("GITHUB_OUTPUT")
    if ruta:
        with open(ruta, "a", encoding="utf-8") as f:
            f.write(f"{clave}={valor}\n")


config = oci.config.from_file(os.path.expanduser("~/.oci/config"))
oci.config.validate_config(config)
tenancy = config["tenancy"]

compute = oci.core.ComputeClient(config)
red = oci.core.VirtualNetworkClient(config)
identidad = oci.identity.IdentityClient(config)

# 1. ¿Ya existe?
for inst in oci.pagination.list_call_get_all_results(compute.list_instances, tenancy).data:
    if inst.display_name == NOMBRE and inst.lifecycle_state not in ("TERMINATED", "TERMINATING"):
        aviso("notice", f"La instancia ya existe ({inst.lifecycle_state}).")
        salida("creada", "true")
        sys.exit(0)

# 2. Datos necesarios: dominio de disponibilidad, subred pública e imagen.
ad = identidad.list_availability_domains(tenancy).data[0].name

subredes = [
    s for s in oci.pagination.list_call_get_all_results(red.list_subnets, tenancy).data
    if s.lifecycle_state == "AVAILABLE" and not s.prohibit_public_ip_on_vnic
]
if not subredes:
    aviso("error", "No hay ninguna subred pública. Creá una VCN con el asistente de Oracle.")
    sys.exit(1)
subred = subredes[0]

imagenes = compute.list_images(
    tenancy,
    operating_system="Canonical Ubuntu",
    operating_system_version="24.04",
    shape=FORMA,
    sort_by="TIMECREATED",
    sort_order="DESC",
).data
imagenes = [i for i in imagenes if "Minimal" not in i.display_name]
if not imagenes:
    aviso("error", "No se encontró la imagen de Ubuntu 24.04 para ARM.")
    sys.exit(1)
imagen = imagenes[0]

print(f"Intentando: {FORMA} {OCPUS:g} OCPU / {MEMORIA_GB:g} GB, {imagen.display_name}, "
      f"subred {subred.display_name}, {ad}")

detalles = oci.core.models.LaunchInstanceDetails(
    compartment_id=tenancy,
    availability_domain=ad,
    display_name=NOMBRE,
    shape=FORMA,
    shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
        ocpus=OCPUS, memory_in_gbs=MEMORIA_GB
    ),
    source_details=oci.core.models.InstanceSourceViaImageDetails(
        image_id=imagen.id, boot_volume_size_in_gbs=DISCO_GB
    ),
    create_vnic_details=oci.core.models.CreateVnicDetails(
        subnet_id=subred.id, assign_public_ip=True
    ),
    metadata={"ssh_authorized_keys": CLAVE_SSH},
)

# 3. Intento.
try:
    inst = compute.launch_instance(detalles).data
except oci.exceptions.ServiceError as e:
    texto = f"{e.code}: {e.message}"
    if e.status == 429:
        aviso("notice", "Oracle frenó por exceso de pedidos. Se reintenta en la próxima ejecución.")
        sys.exit(0)
    if "capacity" in (e.message or "").lower():
        aviso("notice", "Sin capacidad en Santiago. Se reintenta en la próxima ejecución.")
        sys.exit(0)
    if e.status == 400 and "LimitExceeded" in (e.code or ""):
        aviso("error", f"Se superó el límite gratuito de la cuenta. ({texto})")
        sys.exit(1)
    aviso("error", f"Error inesperado de Oracle. ({texto})")
    sys.exit(1)

aviso("notice", f"¡INSTANCIA CREADA! Estado {inst.lifecycle_state}.")
salida("creada", "true")
