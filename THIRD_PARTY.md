# Third-party components

URL Bot source code is distributed under the MIT license in `LICENSE`.
Running `bot.py` uses only the Python standard library.

The standalone Linux executable includes CPython and its native modules.
CPython's license and notices are copied from the build interpreter into
`/usr/share/doc/url-bot/licenses/Python-LICENSE.txt` in the Debian package.
These include the notices for components distributed with CPython.

PyInstaller builds the executable. Its bootloader is covered by the GPL with
an exception that permits distributing bundled applications under their own
licenses. The build includes PyInstaller's full license and exception in
`/usr/share/doc/url-bot/licenses/PyInstaller-COPYING.txt`.

System libraries supplied by Debian or Ubuntu retain their respective
licenses. Building with a different Python distribution can change the
libraries and notices it bundles; review that distribution when making releases.
