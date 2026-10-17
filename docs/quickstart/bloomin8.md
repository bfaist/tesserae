# Quickstart: Bloomin8 Canvas

The Bloomin8 Canvas is a battery colour e-paper frame. Its firmware has a Schedule Pull API: you give it an upstream server, a token and a wake time, and from then on it wakes, asks the server for a picture, paints it and sleeps until the time the server gives back. Tesserae acts as that server. No flashing, no Home Assistant needed.

!!! warning "Built against Bloomin8's spec, not yet tested on a Canvas"
    This path follows Bloomin8's [Schedule Pull API document](https://github.com/ARPOBOT-BLOOMIN8/eink_canvas_home_assistant_component/blob/main/docs/Schedule_Pull_API.md) and has not been run against real hardware yet. If you try it, please report how it went on [issue #344](https://github.com/dmellok/tesserae/issues/344), with the Canvas log if something goes wrong.

## 01 — Add the Canvas in Tesserae

1. **Settings → Devices → Add device**, kind **Bloomin8 Canvas**.
2. The panel defaults to the 13.3" Canvas, 1200×1600 portrait. Pick landscape if the frame hangs sideways; for the smaller Canvas, set the panel to 480×800.
3. Save. Tesserae shows the device's access token once, with the upstream URL and a ready-made command for the next step. The device card can show the token again later.

## 02 — Point the Canvas at Tesserae

The Canvas takes its pull settings over its own local HTTP API, so it has to be awake: press its button, or wake it from the Bloomin8 app. Then, from any machine on the same network (replace `CANVAS_IP`, `TESSERAE`, `TOKEN` and the time).

macOS or Linux:

```sh
curl -X PUT http://CANVAS_IP/upstream/pull_settings \
  -H 'Content-Type: application/json' \
  -d '{
    "upstream_on": true,
    "upstream_url": "http://TESSERAE:8765/api/bloomin8",
    "token": "TOKEN",
    "cron_time": "2026-10-13T08:30:00Z"
  }'
```

Windows PowerShell (the `curl` there is a different program, so use this form):

```powershell
$cron = (Get-Date).ToUniversalTime().AddMinutes(2).ToString("yyyy-MM-ddTHH:mm:ssZ")
$body = @{ upstream_on = $true; upstream_url = "http://TESSERAE:8765/api/bloomin8"; token = "TOKEN"; cron_time = $cron } | ConvertTo-Json
Invoke-RestMethod -Method Put -Uri "http://CANVAS_IP/upstream/pull_settings" -ContentType "application/json" -Body $body
```

`cron_time` is the first wake, in UTC with a trailing `Z`. A minute or two ahead is fine. After that first pull Tesserae sets every wake. To check what the Canvas holds, `GET` the same address (in PowerShell, `Invoke-RestMethod http://CANVAS_IP/upstream/pull_settings`).

`TESSERAE` is the address and port of Tesserae itself, which the token pop-up fills in for you. With the Home Assistant App that is the HA host on the App's port, 8765 unless you changed it, not HA's own port 8123.

The Canvas downloads its picture from the same address, so `upstream_url` must be reachable from the Canvas: plain `http://` on your network works, and `https://` needs a certificate from a public authority (Let's Encrypt is fine). It doesn't follow redirects, so point it at Tesserae directly rather than at a URL that forwards.

## 03 — Bind a dashboard

Bind a page, schedule or rotation to the Canvas like any other device. On each wake the Canvas gets the latest frame as a full-colour JPEG and does its own colour conversion for the panel. A landscape dashboard is sent turned on its side and marked as landscape, which is how the Canvas expects it.

## 04 — Wakes and battery

The device card's **Wake interval** (default 30 minutes) is the longest the Canvas sleeps. When the bound page has a scheduled change coming up, Tesserae wakes it just after the change instead, so a morning dashboard lands at the time it was scheduled for. Manual **Send** and Home Assistant triggers can't be predicted, so they show on the next wake.

When nothing has changed since the last picture the Canvas confirmed painting, Tesserae tells it so and it goes back to sleep without repainting, which saves battery.

The battery level the Canvas reports on each pull shows on the device card, in battery history and in Home Assistant.

## Stopping

Send `{"upstream_on": false}` to the same `pull_settings` address while the Canvas is awake. Deleting the device in Tesserae doesn't stop it: the Canvas keeps waking hourly and getting "unknown token" replies until it is turned off this way.
