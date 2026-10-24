# pyWindowsICSwrapper

A small PySide6 GUI that makes sharing an internet connection on Windows quick and painless, using Windows' built-in Internet Connection Sharing (ICS).

# Features

**Share Internet (Server) tab**
- lists all network adapters with status, description and IPv4 addresses
- auto-detects the adapter with internet (default route) and preselects it
- pick the adapter to share on, the IP to assign it and the prefix length
- enables IP forwarding + ICS, disabling any previous sharing first
- one button to disable all sharing and reset the ICS scope to `192.168.137.1`; optionally sets Google Public DNS on the adapter(s) that were sharing

**Configure Client tab**
- sets a static IP, gateway (the sharing PC) and DNS servers on the client adapter
- one button to return the adapter to DHCP; optionally sets Google Public DNS on that adapter
- connectivity diagnostics: ping the server, ping the internet, DNS lookup

**Reset actions**
The "Use Google DNS when resetting" checkbox (checked by default) controls whether reset actions configure:
- IPv4: `8.8.8.8`, `8.8.4.4`
- IPv6: `2001:4860:4860::8888`, `2001:4860:4860::8844`

**Adapter Metrics tab**
- lists all adapters with their IPv4 interface metric, sorted lowest (preferred) first
- lets you edit the metric of each adapter and apply the changes
- applying a metric disables automatic metric selection for that adapter

Administrator rights are required; the app relaunches itself elevated through UAC when needed.

# Usage

- [end-user] Can be used via the bundled executable (available in [Releases](https://github.com/ageorge95/pyWindowsICSwrapper/releases))
- [end-user] Can be used by running `Install.bat` and after that `START_ICS_Manager.bat`
- [dev] Can be built as an exe by running `Install.bat` and after that `BUILD_release.bat` (the result is in `dist\WindowsICSManager`)

# Requirements

- Windows 10/11 with PowerShell
- Python 3.10+ when running from source

# Support
Found this project useful? Send your ❤ in any form you can 🙂. Please contact me if you donated and want to be added to the contributors list !

- chia XCH---xch1glz7ufrfw9xfp5rnlxxh9mt9vk9yc8yjseet5c6u0mmykq8cpseqna6494
