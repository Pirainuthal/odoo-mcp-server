"""Allow ``python -m odoo_mcp_server``."""

import sys

from .server import main

sys.exit(main())
