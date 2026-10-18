# ZecTrix NOTE4C Devkit

The NOTE4C Devkit is a battery-powered 400 × 300 four-colour e-paper device.
Community firmware by [@varanu5](https://github.com/varanu5) supports
Tesserae REST and Cloud relay.

> **For the four-colour NOTE4C Devkit only. Do not install this firmware on the grayscale NOTE4 variant.**

## Firmware and setup

- [Firmware and instructions](https://github.com/varanu5/zectrix-note4c-devkit-tesserae-client)
- [Web installer](https://note4ctesserae.pages.dev) (desktop Chrome or Edge)

Backing up the original firmware is strongly recommended before the first installation.
Follow the firmware repository's flashing guide, then use the device's Wi-Fi setup
portal to select REST or Cloud relay and enter the corresponding connection details.
For REST, use a server address reachable by the device. For Cloud relay, use the
relay URL and pairing code provided by your relay setup.

Select **ZecTrix NOTE4C Devkit** when choosing the device type in Tesserae.
Assign a page or lineup after pairing. Hold the Front button for 20 seconds
to reopen device setup when needed.

## Display and sleep settings

| Property | Value |
|---|---|
| Resolution | 400 × 300, landscape |
| Colours | Black, white, red and yellow (`bwry_4`) |
| Renderer | `esp32_bin`, packed 2 bits per pixel (30,000 bytes) |
| Device kind | `note4c_client` |
| Default sleep interval | 15 minutes (900 seconds) |
| Configurable interval | 30 seconds to 7 days |

Change the sleep interval in the server's device settings. Explicit saved intervals
take precedence over the default. New settings are received when the device next
contacts the server. A short Front press wakes the device for a check.

The firmware handles panel refresh modes and battery protection. The device
reports battery voltage, estimated charge, Wi-Fi signal and firmware version.
