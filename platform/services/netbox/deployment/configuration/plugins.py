from os import environ

PLUGINS = ["netbox_diode_plugin"]

# Read the netbox-to-diode client secret from the mounted secrets file.
# The file is bind-mounted from secrets/netbox_to_diode_client_secret.txt
# at /run/secrets/netbox_to_diode (see docker-compose.yml).
_netbox_to_diode_secret = ""
try:
    with open("/run/secrets/netbox_to_diode", "r") as _f:
        _netbox_to_diode_secret = _f.readline().strip()
except OSError:
    pass

PLUGINS_CONFIG = {
    "netbox_diode_plugin": {
        "diode_target_override": environ.get(
            "DIODE_TARGET_OVERRIDE", "grpc://ingress-nginx:80/diode"
        ),
        "diode_username": "diode",
        "netbox_to_diode_client_secret": _netbox_to_diode_secret,
    }
}
