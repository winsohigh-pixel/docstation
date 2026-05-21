# DocStation Linux v6

Fixes native GTK startup hardening:

- Login role selection no longer crashes even if called before password field construction.
- `run_app.sh` refuses to start from `~/.local/share/Trash` to prevent launching stale deleted copies.
- Added `scripts/self_check.sh` for syntax, GTK namespace and init checks.
- Startup exceptions are written to `logs/native_startup_error.log`.

Clean install recommended:

```bash
cd ~/Downloads
rm -rf docstation_linux
unzip docstation_linux_v6_native_startup_verified.zip
cd docstation_linux
chmod +x scripts/*.sh
./scripts/install_ubuntu.sh
./scripts/self_check.sh
./scripts/run_app.sh
```
