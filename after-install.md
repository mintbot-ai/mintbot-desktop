# mintbot Desktop installed

With an AXP host, open the extension's **Agent desktop** view in the panel.
The host can pin it beside chat or show it fullscreen. The installed
`mintbot-desktop` skill controls the same graphical session using the launcher.

For a standalone installation, run the printed `bin/mintbot-desktop serve`
command (default `/opt/mintbot-desktop/bin/mintbot-desktop serve`), or run
`./enable.sh` from the retained artifact. Both run in the foreground; neither
installs a service. See README for an operator-owned supervisor and authenticated
HTTP/WebSocket proxy setup. There is no standalone host panel.

On a server this is a root-capable desktop of the package's own with access
to this agent server, not a sandbox for untrusted pages. On your own Linux
computer, with you logged in at an X11 desktop, the view shows **your own
screen**: whoever opens it sees and operates your computer, and the agent's
programs appear on your desktop (a Wayland login gets the package's own
desktop instead). Chromium uses a separate profile; the manifest declares
unrestricted outbound browsing. The ordinary Hermes browser tool does not
automatically join this session.

Save work before stopping. The host stops its own desktop service; standalone
users must stop their foreground runner or chosen supervisor before disable or
uninstall. Portable hooks never stop host services and refuse cleanup while the
session lock is held. Standalone uninstall removes the owned install prefix;
both modes retain state unless you explicitly choose `AXP_PURGE=1`.
Never share the service token, VNC credentials, Xauthority or browser profile.
