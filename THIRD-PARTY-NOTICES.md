# Third-party notices

The Stashler desktop executable bundles the following open-source libraries. They are
included unmodified; their licenses and source are linked below.

| Library | License | Source |
|---------|---------|--------|
| Flask | BSD-3-Clause | https://github.com/pallets/flask |
| httpx | BSD-3-Clause | https://github.com/encode/httpx |
| websockets | BSD-3-Clause | https://github.com/python-websockets/websockets |
| Pillow | HPND (PIL Software License) | https://github.com/python-pillow/Pillow |
| **pystray** | **LGPL-3.0** | https://github.com/moses-palmer/pystray |

These pull in transitive dependencies (e.g. Jinja2, Werkzeug, MarkupSafe, click,
itsdangerous, blinker, certifi, httpcore, h11, idna, sniffio, anyio) under similar
permissive licenses (BSD / MIT / MPL-2.0 / Apache-2.0).

**pystray** is the only copyleft component. It is licensed under the GNU LGPL v3, included
unmodified, with its source available at the link above. A copy of the LGPL-3.0 / GPL-3.0
license texts is at https://www.gnu.org/licenses/.

The executable is produced with **PyInstaller**, whose bootloader carries a GPL license with
an exception that explicitly permits distributing the resulting executable under your own
terms.
