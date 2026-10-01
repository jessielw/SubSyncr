# Third-party software

SubSyncr's own code is MIT licensed; see `LICENSE`. Dependencies retain their
respective licenses. Release bundles include `THIRD_PARTY_INVENTORY.json`, exact
installed versions and package metadata, and a `licenses/` directory copied from
collected distributions plus the supplemental notices under `packaging/licenses`.
In a Windows/Linux bundle these documents are in `_internal/`; in a macOS app they
are in `Contents/Resources/`. The build does not fetch license documents online.

## Qt and Python

The GUI uses [Qt for Python / PySide6](https://doc.qt.io/qtforpython-6/licenses.html)
and Shiboken, with Qt shared libraries. The open-source LGPLv3 license text and
GPLv3 text it incorporates are included. Qt's own third-party attribution index
is included as `licenses/Qt-third-party.html`. Consult the inventory for exact
versions; source releases are available from
[Qt's archive](https://download.qt.io/archive/qt/) and
[Qt for Python's archive](https://download.qt.io/official_releases/QtForPython/).

The bundled Qt libraries remain shared libraries. A compatible rebuilt library
can replace the corresponding library under `_internal/PySide6` on Windows/Linux,
or inside the `.app` on macOS. Keep an original copy of the app first; locally
modified macOS bundles may need to be ad-hoc signed again. SubSyncr imposes no
additional restriction on modifying those libraries or debugging such changes.

Python is distributed under the Python Software Foundation license and the
additional notices in `licenses/Python.txt`. Its source is available from
[python.org](https://www.python.org/downloads/source/).

## Icons and fonts

QtAwesome is copyright The Spyder development team (MIT). Its font files are
preserved unmodified, including embedded attribution. The application uses
Phosphor icons; QtAwesome also loads its other bundled font families:

| Family | Attribution / terms | Included notice |
| --- | --- | --- |
| Phosphor | Phosphor Icons, MIT | `Phosphor.txt` |
| Font Awesome 5 and 6 | Fonticons, Inc., SIL OFL 1.1 fonts | `Font-Awesome-5.txt`, `Font-Awesome-6.txt` |
| Elusive Icons | Team Redux, SIL OFL 1.1 fonts | `Elusive-Icons.txt`, `OFL-1.1.txt` |
| Material Design Icons | Pictogrammers, Apache 2.0 fonts | `Material-Design.txt`, `Remix-Icon.txt` (Apache text) |
| Remix Icon 2.5 | Remix Design, Apache 2.0 | `Remix-Icon.txt` |
| Codicons | Microsoft, CC BY 4.0 | `Codicons.txt` |

Upstream locations of supplemental documents are recorded in
`packaging/licenses/sources.json`. Font versions are recorded in their bundled
filenames. The original SubSyncr application mark is covered by the project license.

The faust-cchardet wheel omits its license file; its upstream `COPYING` is
included separately as `licenses/faust-cchardet-COPYING.txt`. PySoundFile's generic
wheel also omits its license; `licenses/PySoundFile-LICENSE.txt` is copied verbatim
from the exact source archive pinned in `uv.lock`. Its source URL and archive
checksum are recorded in `packaging/licenses/sources.json`. Other
collected Python distributions supply their own notices in the inventory. Builds
fail if a collected distribution has neither a packaged nor supplemental notice.

## Synchronization engines

ffsubsync and autosubsync are bundled Python dependencies (MIT); their notices are
collected from their distributions. This release uses the ffsubsync fork identified
in `pyproject.toml` and `uv.lock`.

[alass](https://github.com/kaegi/alass) and
[lapse](https://github.com/Schwponaco-org/lapse) are separate native programs,
downloaded from their upstream releases on demand. They are not contained in the
SubSyncr archives. Their own licenses and any accompanying ffmpeg build apply to
those downloads. SubSyncr also does not bundle the user's ffmpeg installation.

When changing dependency versions, review the generated inventory and supplemental
notices against the files actually shipped; see `docs/RELEASING.md`.
