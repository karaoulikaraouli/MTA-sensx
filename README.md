[README.md](https://github.com/user-attachments/files/34843161/README.md)
# MTA Per-Weapon Sensitivity

A Windows tool for Multi Theft Auto: San Andreas that lets you set a different mouse sensitivity multiplier for each weapon. Set your values in the window, click **Apply**, and hold right-click in MTA to use the multiplier for the weapon you have equipped.

The multiplier applies to relative mouse movement. A setting of 1.44× means 100 mouse counts become approximately 144 counts over time. Releasing right-click returns movement to 1×. Switching away from MTA also returns movement to 1×. The tool reads game memory to identify your weapon and uses the Windows Interception driver to process mouse movement.

## Setup

1. Use Windows and 64-bit Python with Tkinter. The bundled DLL is 64-bit. The script uses Python's standard library and has no pip dependencies.
2. Install the [Interception driver](https://github.com/oblitum/Interception/releases) if it is missing. Extract the upstream release, open an administrator Command Prompt in its `command line installer` folder, run `install-interception.exe /install`, and restart Windows. The bundled DLL is the user-mode interface to the separately installed driver.
3. Extract the project files together. Keep the DLL and all thirteen images beside the script.
4. Start one MTA client and join a local test server. If you used the old Raw Accel script, close it. For an isolated comparison, set Raw Accel to 1× and turn acceleration off.
5. Open Command Prompt in the project folder and run:

   ```cmd
   python test_mouse_scale.py
   ```

6. Set your multipliers and click **Apply**, then click **Enable filter**. With MTA focused, equip a weapon and hold right-click. Press **F10**, click **Stop filter**, or close the window to stop.

The script assumes right mouse button is your aim bind. Starting the filter with that button released lets it observe the next complete press and release.

## The window and saved settings

| Control | What it does |
|---|---|
| Weapon slider | Sets a draft multiplier between 0.10× and 6.00×. The label displays two decimal places. |
| Apply | Rounds all slider values to two decimal places, saves them, and gives the running filter the new settings. |
| Reset all to 1× | Sets every slider to 1.00× and immediately applies and saves it. |
| Enable filter / Stop filter | Starts the background worker or asks it to stop. |
| F10 | Stops the worker through a Windows key-state check. The settings window stays open. |

`sensitivity.json` is created next to the script when settings are first saved. It is a JSON object keyed by weapon ID. For example, `"28": 1.44` is the Micro Uzi multiplier. The real file contains one entry per slider. The app loads supported keys in the slider range at startup. Missing keys use their built-in defaults. If the file cannot be read or parsed, the status line reports that and the window uses defaults.
Tiki.

Apply writes `sensitivity.tmp` first and then replaces `sensitivity.json`. If saving fails, the old live settings remain in use and the window displays the error. The app folder therefore needs to be writable.

## Files and weapon IDs

- `test_mouse_scale.py` contains the UI, settings handling, weapon reader, and mouse filter.
- `interception.dll` sends requests to the installed Interception driver.
- The PNG files are the weapon icons displayed in the settings window. They are loaded by filename from the script folder.
- `sensitivity.json` is generated locally. It can be omitted from a fresh checkout or ignored in Git.

| ID | Weapon | Image | First-run multiplier |
|---:|---|---|---:|
| 22 | Colt 45 | `Colt45.png` | 1.00× |
| 24 | Desert Eagle | `Deagle.png` | 1.00× |
| 25 | Shotgun | `Shotgun.png` | 1.00× |
| 26 | Sawed-off | `Sawnoff.png` | 1.00× |
| 27 | SPAS-12 | `Spas12.png` | 1.00× |
| 28 | Micro Uzi | `Mac10.png` | 1.44× |
| 29 | MP5 | `Mp5.png` | 1.00× |
| 32 | Tec-9 | `Tec9.png` | 6.00× |
| 30 | AK-47 | `Ak47.png` | 1.00× |
| 31 | M4 | `M4.png` | 1.00× |
| 33 | Rifle | `Rifle.png` | 1.00× |
| 34 | Sniper | `Sniper.png` | 0.10× |
| 38 | Minigun | `Minigun.png` | 1.00× |

Weapon IDs missing from the table use 1×. To add a slider, add an entry to `WEAPONS` and supply its image. Add its ID to `MULTIPLIERS` if you want a default other than 1×.

## How the code works

### 1. Attach to the game

`find_game_pid()` runs PowerShell's `Get-Process` for `gta_sa` and `gta-sa` and requires exactly one match. `OpenProcess` requests `PROCESS_VM_READ` (`0x0010`). The script calls `ReadProcessMemory` for game data and `CloseHandle` during cleanup. It performs zero game memory writes.

`read_memory()` allocates a buffer of the requested size, checks the Windows API result, and checks the number of bytes copied. It raises an error on a failed or incomplete read.

### 2. Read the equipped weapon

The reader uses the classic GTA San Andreas 1.0 US layout. It interprets pointers and weapon IDs as little-endian integers.

| Address or offset | Meaning |
|---|---|
| `0xB7CD98` | Address holding the four-byte local player ped pointer. A zero pointer returns `None`. |
| `ped + 0x5A0` | Start of the weapon array. |
| `ped + 0x718` | One-byte active weapon slot. Valid slots are 0–12. |
| `0x1C` | Size of one weapon entry in bytes. |

After reading the pointer separately, `equipped_weapon_id()` reads 377 bytes from the start of the weapon array through the selected-slot byte. This includes the whole weapon array and intervening bytes. It avoids a separate slot read followed by an inventory read. It then takes the first four bytes of the selected entry at `slot * 0x1C` inside that buffer.

While the game owns the foreground window, the worker polls for a weapon about every 10 ms. It commits a weapon change only after two consecutive matching samples. Until then, it keeps the last confirmed weapon. This filters a single bad sample and adds about one polling interval when a weapon changes. It cannot correct a sustained wrong reading.

### 3. Receive mouse packets

`MouseStroke` matches the Interception DLL's mouse packet layout. It contains state and movement flags, wheel data, signed X/Y movement counts, and an information field.

`run_filter()` loads `interception.dll` and binds its functions with `ctypes`. It creates an Interception context and sets a mouse-only filter mask of `0x100C`, which requests movement (`0x1000`), right-button down (`0x0004`), and right-button up (`0x0008`). The worker waits up to 10 ms for an input packet before checking its state again.

The worker tracks right-button state separately for each Interception device. It also reads the foreground window and its process ID through `user32.dll`. This is a foreground-process and physical-button check. It does not read MTA's internal aim state.

### 4. Scale and forward the movement

The worker changes a packet's X and Y counts only when all of these conditions hold:

- The packet is from a device whose right mouse button is held.
- Movement is relative. Absolute-position packets are skipped.
- The foreground window belongs to the GTA process, and the window is still the same immediately before scaling.
- The confirmed weapon has a multiplier other than 1.00×.

`scale_axis()` calculates `counts × multiplier + previous remainder`. It truncates toward zero and carries the fractional remainder to the next packet on that axis and device. At 0.5×, two successive packets of +1 count can produce 0 and then +1 instead of losing all movement to rounding. Integer output is clamped to the signed 32-bit range. Remainders are cleared when settings, the foreground window, or the confirmed weapon change. Releasing right-click clears that device's remainder.

The worker forwards each received packet through `interception_send`. It changes only relative X/Y movement. Button flags, wheel data, and other packet fields pass through unchanged. There is no targeting, recoil correction, or automatic firing. The code sends movement as packets arrive and has no per-change Raw Accel writer call or deliberate one-second wait.

### 5. Keep the UI responsive and clean up

`SensitivityWindow` creates the Tkinter controls and keeps references to the images. The main thread handles the UI. A background thread runs the filter, and a queue sends status messages back to the window. The window checks that queue every 50 ms. That refresh interval affects only the status display.

Apply publishes a fresh settings dictionary after a successful save. The worker reads the new dictionary on its next loop. On stop or an error, cleanup removes the filter mask, destroys the Interception context, and closes the process handle. Closing the window signals the worker and waits for it to finish before destroying the UI.

## What to test

1. Use one weapon on a local server and move the mouse a fixed distance with right-click released.
2. Repeat with right-click held at 1× and at a clearly different multiplier. Compare camera rotation in the same aim mode.
3. Release right-click while moving, switch to a weapon with a different multiplier, and repeat. Check that the change follows the right button and the equipped weapon.
4. Alt+Tab while holding right-click, check that desktop movement stays at 1×, and stop the filter with F10.
5. Click **Reset all to 1×**, reopen the window, and check that the sliders are still at 1×.

The fixed right-click scaling test worked on the original setup. The UI, image loading, Apply, Reset, and saved settings were checked separately. The packaged UI and weapon-reading combination still needs an in-game test.

## Troubleshooting and limits

| Symptom | What to check |
|---|---|
| The filter reports Interception is unavailable | Make sure the Interception driver is installed and Windows has restarted. The DLL alone cannot process packets. |
| Windows cannot load the DLL | Keep `interception.dll` beside the script and use Python with the matching architecture. |
| The process is missing or ambiguous | Start one GTA/MTA client before enabling the filter. Close any second copy. |
| A memory read fails or the slot is invalid | The reader uses fixed offsets. Check the GTA build and retry after spawning. The worker stops and shows the error on a failed read. After restarting MTA, stop and enable the filter again to attach to the new process. |
| A weapon reads as the wrong ID | The status shows the confirmed weapon name when it is listed. The two-read check filters brief changes. A stable incorrect result requires checking the game build or reported weapon ID. |
| Scaling triggers in an MTA menu | Menus can belong to the same foreground process. The script checks right-click, so menu right-click can also trigger scaling with a known weapon. |
| Moving a slider does nothing | Click Apply, enable the filter, focus MTA, and hold right-click with a configured weapon. |
| Apply reports a save error | Extract into a writable folder and confirm that `sensitivity.json` can be replaced. |

The script does not verify the executable version automatically. Driver compatibility and timing depend on the system. Read-only game access and mouse packet forwarding do not establish that a particular server allows the tool. Ask the server staff before using it online.

The Python source can be reviewed independently of the bundled DLL. See the [upstream Interception project](https://github.com/oblitum/Interception) for the driver and DLL source information, licensing terms, and release downloads. Check the applicable redistribution terms for the DLL and weapon images before publishing them. The Python project's own license should be stated separately if one is chosen.
Coded by Tiki.
