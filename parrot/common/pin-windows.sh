#!/usr/bin/env bash
# Make window geometry deterministic before the window manager starts.
#
# Copied from applications/videoplayers/common/pin-windows.sh, which came from
# applications/terminals/common/; the file is group-agnostic and this group
# needs the same guarantees - pinned geometry, no window-manager key grabs, no
# toolbar clock in the capture.
#
#   pin-windows.sh <main-window-res-name> [width] [height] [res-class] [role]
#   pin-windows.sh Navigator 1440 900               # Firefox - the res_name
#                                                   # alone; see the note on
#                                                   # MAIN_ROLE below
#   pin-windows.sh "" 1440 900 parrot-chrome        # Chrome - by class, see
#                                                   # below
#
# ONE DIVERGENCE FROM THE OTHER COPIES: the main-window rule can be written
# against the res_CLASS with an empty res_name. Chrome needs that. Its res_name
# is built from the binary name and the profile directory -
#
#   ('chromium-browser (/tmp/parrot-profile-measure)', 'parrot-chrome')
#
# - so a res_name rule would have to contain a filesystem path, and fluxbox
# matches these patterns in full rather than as a substring: (name=chromium-browser)
# was measured here and does NOT match that window. Chrome's res_class, on the
# other hand, is whatever --class says, and websites/common/launch-browser.sh
# sets it to parrot-chrome for exactly this reason.
#
# Must run BEFORE entrypoint.sh, because fluxbox reads ~/.fluxbox/apps once at
# startup and will not overwrite a file that is already there.
#
# WHY THIS EXISTS
#
# fluxbox 1.3.7 leaves session.screen0.windowPlacement unset, so it falls back to
# RowSmartPlacement: each new window is positioned to avoid overlapping the
# windows already on screen.  That makes position a function of whatever else
# happens to be open, which is exactly what you do not want in a recording.
#
# It compounds, because a dialog is centred on its parent:
#
#   main window        placed by the app, then resized by position-window.sh
#   compose window     placed by RowSmartPlacement, relative to the main window
#   SMTP password box  centred on the compose window
#
# So the outgoing-server password prompt inherits two layers of variability, and a
# macro that clicks its OK button at fixed coordinates misses.  That is what broke
# the last three steps of the Thunderbird recording.
#
# Fixing it here rather than in the macro matters: window management done with
# xdotool is NOT an input event, so xmacrorec2 does not record it and replay would
# never repeat it.  The window manager, on the other hand, applies these rules
# identically at record time and at replay time - both runs go through the same
# setup-commands in the same image.
set -euo pipefail

# The main window's res_name - the FIRST field of WM_CLASS, e.g. "Mail" for
# Thunderbird's 3-pane.  Optional: without it every window including the main one
# is merely centred, which is still fully reproducible, just not fullscreen.
#
# To find it for a client, run it and read the property off the window:
#   apt-get install -y x11-utils      # xprop is not in the image
#   DISPLAY=:99 xprop -id "$(xdotool search --onlyvisible --name '<title>' | head -1)" WM_CLASS
# Thunderbird's 3-pane reports "Mail", "thunderbird-esr"; its compose window
# reports "Msgcompose" and its dialogs "Thunderbird", all sharing the res_class.
MAIN_NAME="${1:-}"
# WIDTH=0 means "undecorate and position, but do NOT force the size", and it
# exists because forcing the size is not harmless for every toolkit.
#
# Celluloid, GTK4 with libmpv rendering through a GL surface, goes COMPLETELY
# BLACK when fluxbox applies [Dimensions] to it: the position bar advances, the
# clip plays, and the video area stays black for the whole run. Measured by
# splitting the rule in half - [Deco] {NONE} alone renders normally, [Dimensions]
# alone reproduces the black window. Nothing is logged either way.
#
# It costs nothing here because the corpus is exactly screen-sized, so a player
# that sizes itself to its video already lands on 1440x900, and the one clip that
# does not is handled by that entrant's own autofit.
WIDTH="${2:-1440}"
HEIGHT="${3:-900}"
# Optional res_class, for clients where the res_name alone is ambiguous.
# BlueMail needs it: it maps a 20x20 tray window that shares the res_name
# "bluemail" with the main window and differs only in res_class ("Bluemail"
# against the main window's "BlueMail").  Without the extra term the tray window
# matches too and fluxbox blows it up to a full-screen, undecorated 1440x900 that
# covers the mailbox.
MAIN_CLASS="${4:-}"
# Optional WM_WINDOW_ROLE, for clients whose dialogs are indistinguishable from
# the main window by WM_CLASS alone - which is most of them.
#
# Claws Mail needs it.  Its password prompt, its progress windows and its
# compose window all report WM_CLASS "claws-mail", "Claws-mail", so without a
# role the main-window rule below matches them too and moves them to 0,0 - where
# fluxbox stacks them UNDER the full-screen main window.  The prompt is then
# invisible and modal: the folder tree stops responding to clicks and the client
# looks like it has hung.  Only the main window carries
# WM_WINDOW_ROLE = "mainwindow", so naming it confines the rule to that window
# and every dialog falls through to the centring rule instead.
#
# Read it off a running client with:
#   DISPLAY=:99 xprop -id "$(xdotool search --onlyvisible --name '<title>' | head -1)" \
#       WM_CLASS WM_WINDOW_ROLE
MAIN_ROLE="${5:-}"

# ONLY (name=...) IS RELIABLE IN THIS IMAGE. Measured against LibreOffice Writer
# 24.2.7, whose document window reports WM_CLASS "libreoffice",
# "libreoffice-writer" and no WM_WINDOW_ROLE:
#
#   (name=libreoffice)          pinned    0,0 1440x900, decorations gone
#   (class=libreoffice-writer)  no match  0,44 1440x875
#   (class=libreoffice)         no match  0,44 1440x875
#   (name=libreoffice-writer)   no match  0,44 1440x875
#   (title=.*Writer)            no match  0,44 1440x875
#
# fluxbox evaluates an [app] rule when the window is MAPPED, and at that moment
# LibreOffice has set its res_name but not yet its final res_class or title -
# both of which it fills in once it knows which module owns the document. So a
# rule written against the properties you read off a settled window matches
# nothing, silently, and the window is simply left where the app put it.
#
# Read the properties, then confirm the rule actually fires by checking the
# geometry. A rule that does not match is invisible: no error, no warning, just
# an unpinned window and reference screenshots taken at the wrong size.
#
# Dialogs that share the main window's res_name are not a problem here even
# though the rule matches them too: fluxbox centres a window with
# WM_TRANSIENT_FOR on its parent regardless of [Position], and Writer's dialogs
# additionally carry a program-specified maximum size, so [Dimensions] is
# clamped. Find and Replace stays 574x403 centred with the full-screen rule
# active. An app whose dialogs are NOT transient and carry no maximum size would
# need a discriminator - and would need one that works at map time.

log() { printf '[pin-windows] %s\n' "$*"; }

# Refuse a quoting artefact rather than writing a rule that matches nothing.
#
# GMT splits a setup-command with shlex.split(posix=False), which KEEPS quote
# characters. So `pin-windows.sh "" 1440 900 parrot-chrome` written without
# `shell: bash` in the usage_scenario arrives here with MAIN_NAME set to the
# two-character string `""`, and the rule below becomes (name="") - valid
# syntax, matching no window, reported by nobody. Fail here instead.
for _arg_name in MAIN_NAME MAIN_CLASS MAIN_ROLE; do
    case "${!_arg_name}" in
        *'"'*|*"'"*)
            echo "[pin-windows] FATAL: ${_arg_name} is ${!_arg_name} - it still contains quote characters." >&2
            echo "[pin-windows] A usage_scenario setup-command that passes an empty argument needs 'shell: bash'," >&2
            echo "[pin-windows] otherwise GMT keeps the quotes and the window-manager rule matches nothing." >&2
            exit 2
            ;;
    esac
done

FB="${HOME}/.fluxbox"
mkdir -p "$FB"

# --- keys: no keyboard grabs at all ---------------------------------------
#
# fluxbox's default keys file binds Control+F1..F12 to "switch to workspace N".
# It grabs those combinations at the X server, so the application never sees
# them - and Ctrl+F12 is Insert Table in LibreOffice Writer.
#
# The failure is silent and looks like a crash. Ctrl+F12 sent to Writer moves
# the display to workspace 12, which is empty: `xdotool search` returns nothing,
# `import -window root` comes back fully transparent, and the soffice processes
# are still running. Nothing in any log mentions it. Ctrl+F1 brings it back.
#
# It would happen identically on replay, so it cannot be worked around in the
# driver - the keystroke has to reach the application.
#
# The default file also grabs Mod1+F1/F2/F4/F5/F9/F10/F11 and Mod1+space. Rather
# than strip the twelve dangerous lines and leave the rest to be discovered one
# application at a time, this writes a keys file with NO keyboard bindings
# whatsoever. Mouse bindings are kept so fluxbox still behaves like a window
# manager. Nothing in a recording uses a window-manager keybinding: the whole
# point is that every keystroke belongs to the application under test.
#
# fluxbox will not overwrite an existing keys file, and this runs before
# fluxbox starts, so this is the file it reads.
cat > "${FB}/keys" <<'KEYS'
OnDesktop Mouse1 :HideMenus
OnDesktop Mouse2 :WorkspaceMenu
OnDesktop Mouse3 :RootMenu
OnTitlebar Mouse1 :MacroCmd {Focus} {Raise} {ActivateTab}
OnTitlebar Move1 :StartMoving
OnLeftGrip Move1 :StartResizing bottomleft
OnRightGrip Move1 :StartResizing bottomright
OnWindowBorder Move1 :StartMoving
KEYS

# Deterministic placement for anything the rules below do not cover.
# CascadePlacement depends only on how many windows are already mapped, which is
# reproducible for a fixed sequence of actions; RowSmartPlacement depends on their
# sizes and positions, which is not.
#
# The toolbar is hidden for two reasons.  It reserves a 45 px strut at the bottom
# of the screen, and fluxbox clamps a full-height window to the area left over -
# so a window asked for 1440x900 silently becomes 1440x855, every y coordinate
# below the fold shifts, and clicks aimed at the bottom of the window miss.  It
# also carries a live clock, which is a moving pixel in any full-screen capture.
cat > "${FB}/init" <<INIT
session.menuFile:	~/.fluxbox/menu
session.keyFile:	~/.fluxbox/keys
session.styleFile:	/usr/share/fluxbox/styles//ubuntu-light
session.configVersion:	13
session.screen0.windowPlacement:	CascadePlacement
session.screen0.rowPlacementDirection:	LeftToRight
session.screen0.colPlacementDirection:	TopToBottom
session.screen0.fullMaximization:	true
session.screen0.toolbar.visible:	false
INIT

# The main window fills the screen with no decorations, so the client area starts
# at exactly 0,0 - no title-bar offset to account for in click coordinates.
#
# Everything else is centred on the *screen* rather than on its parent, which
# breaks the inheritance chain described above: a dialog lands in the same place
# whatever its parent is doing.
{
    if [[ -n "$MAIN_NAME" || -n "$MAIN_CLASS" ]]; then
        main_pattern=""
        [[ -n "$MAIN_NAME" ]]  && main_pattern="(name=${MAIN_NAME})"
        [[ -n "$MAIN_CLASS" ]] && main_pattern="${main_pattern:+${main_pattern} }(class=${MAIN_CLASS})"
        [[ -n "$MAIN_ROLE" ]]  && main_pattern="${main_pattern} (role=${MAIN_ROLE})"
        if [[ "$WIDTH" == "0" ]]; then
            cat <<MAINAPP
[app] ${main_pattern}
  [Deco]	{NONE}
  [Position]	(TOPLEFT)	{0 0}
[end]

MAINAPP
        else
            cat <<MAINAPP
[app] ${main_pattern}
  [Deco]	{NONE}
  [Dimensions]	{${WIDTH} ${HEIGHT}}
  [Position]	(TOPLEFT)	{0 0}
[end]

MAINAPP
        fi
    fi
    cat <<APPS
[app] (name=fbrun)
  [Position]	(WINCENTER)	{0 0}
  [Layer]	{2}
[end]

[app] (title=.*)
  [Position]	(CENTER)	{0 0}
[end]
APPS
} > "${FB}/apps"

if [[ -n "$MAIN_NAME" || -n "$MAIN_CLASS" ]]; then
    if [[ "$WIDTH" == "0" ]]; then
        log "main window '${main_pattern}' undecorated at 0,0; size left to the app"
    else
        log "main window '${main_pattern}' pinned to ${WIDTH}x${HEIGHT} at 0,0, no decorations"
    fi
else
    log "no main-window name or class given; every window is merely centred"
fi
log "all other windows centred on the screen; placement policy CascadePlacement"
