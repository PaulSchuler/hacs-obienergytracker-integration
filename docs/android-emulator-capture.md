# Live-Daten mitschneiden: Android-Emulator + mitmproxy

Anleitung, um den Traffic der heyOBI-App auf einem Windows-PC mitzulesen und
den Endpunkt zu finden, über den die App ihre Live-Leistung bezieht.

Der Emulator wird gerootet, damit das mitmproxy-Zertifikat in den
**System**-Zertifikatsspeicher kann. Nur so vertraut die App ihm — seit
Android 7 ignorieren Apps selbst installierte CAs. Das erspart das Patchen
und Neusignieren der APK.

Veranschlagte Zeit: ca. eine Stunde beim ersten Mal.

> Vorher lohnt sich `python tools/probe_obi_api.py --live-session`. Wenn die
> Live-Daten über einen REST-Endpunkt laufen, findet das Skript ihn in zehn
> Minuten und die ganze Anleitung hier entfällt.

---

## 1. Werkzeuge installieren

**Android SDK** — entweder Android Studio installieren oder nur die
[Command line tools](https://developer.android.com/studio#command-line-tools-only)
nach `%LOCALAPPDATA%\Android\Sdk\cmdline-tools\latest` entpacken.

Umgebungsvariablen setzen (PowerShell, dauerhaft):

```powershell
[Environment]::SetEnvironmentVariable("ANDROID_HOME", "$env:LOCALAPPDATA\Android\Sdk", "User")
[Environment]::SetEnvironmentVariable("ANDROID_SDK_ROOT", "$env:LOCALAPPDATA\Android\Sdk", "User")
```

Danach PowerShell neu öffnen und `$env:ANDROID_HOME\platform-tools`,
`$env:ANDROID_HOME\emulator` und `$env:ANDROID_HOME\cmdline-tools\latest\bin`
in den PATH aufnehmen (oder immer mit vollem Pfad arbeiten).

**Hardware-Beschleunigung**: In „Windows-Features aktivieren" die
*Windows-Hypervisor-Plattform* einschalten, sonst kriecht der Emulator.

**mitmproxy**:

```powershell
pip install mitmproxy
```

---

## 2. System-Image wählen und installieren

```powershell
sdkmanager "platform-tools" "emulator" "system-images;android-30;google_apis;x86_64"
```

Zwei Entscheidungen, die wichtig sind:

- **`google_apis`, nicht `google_apis_playstore`.** Play-Store-Images haben ein
  gesperrtes `/system`, `adb root` verweigert dort den Dienst — und genau das
  brauchen wir. Google Play *Services* (für den Login) sind im
  `google_apis`-Image trotzdem enthalten, nur der Store fehlt.
- **API 30 (Android 11).** Der Zertifikatsspeicher liegt hier noch unter
  `/system/etc/security/cacerts`. Ab Android 14 wandert er in einen
  APEX-Container und wird deutlich fummeliger. Außerdem können die
  x86_64-Images ab Android 11 ARM-Binaries übersetzen — praktisch, falls es
  die heyOBI-APK nur für arm64 gibt.

AVD anlegen:

```powershell
avdmanager create avd -n obi -k "system-images;android-30;google_apis;x86_64" -d pixel_4
```

---

## 3. mitmproxy starten

```powershell
mitmweb --listen-port 8080
```

`mitmweb` öffnet eine Weboberfläche auf <http://127.0.0.1:8081> — praktisch
zum Durchklicken der Flows. Beim ersten Start legt mitmproxy sein Zertifikat
unter `%USERPROFILE%\.mitmproxy\mitmproxy-ca-cert.pem` an. Lass das Fenster
offen.

---

## 4. Emulator starten

```powershell
emulator -avd obi -writable-system -no-snapshot -http-proxy http://127.0.0.1:8080
```

`-writable-system` ist zwingend, sonst lässt sich `/system` später nicht
beschreiben. Und der Emulator muss **jedes Mal** mit diesem Flag starten —
ohne ihn ist das System-Zertifikat wieder weg.

Falls die App später „keine Internetverbindung" meldet, setz den Proxy
stattdessen manuell im System: Settings → Network → Internet → AndroidWifi →
Bearbeiten → Erweitert → Proxy manuell → `10.0.2.2` : `8080`. Die `10.0.2.2`
ist aus Emulator-Sicht der Host.

---

## 5. Zertifikat als System-CA einspielen

Android erwartet Zertifikate unter dem alten OpenSSL-Subject-Hash als
Dateinamen. Den ermittelst du in **Git Bash** (dort ist openssl dabei):

```bash
openssl x509 -inform PEM -subject_hash_old -in ~/.mitmproxy/mitmproxy-ca-cert.pem | head -1
# -> z.B. c8750f0d

cp ~/.mitmproxy/mitmproxy-ca-cert.pem c8750f0d.0
```

Dann einspielen:

```bash
adb root
adb remount
adb push c8750f0d.0 /system/etc/security/cacerts/
adb shell chmod 644 /system/etc/security/cacerts/c8750f0d.0
adb reboot
```

**Wenn `adb remount` fehlschlägt** (Verified Boot funkt dazwischen):

```bash
adb root
adb shell avbctl disable-verification
adb reboot
# warten, dann erneut:
adb root
adb remount
```

**Kontrolle:** Settings → Security → Encryption & credentials → Trusted
credentials → Reiter **System** → dort muss „mitmproxy" auftauchen. Steht es
nur unter „User", hat der Push nicht gegriffen und die App wird das
Zertifikat ablehnen.

---

## 6. heyOBI installieren

Die APK von [APKMirror](https://www.apkmirror.com/) laden. Variante `x86_64`
oder `universal` bevorzugen; `arm64-v8a` läuft auf dem API-30-x86_64-Image
dank Übersetzung meist auch, nur langsamer.

```bash
adb install -r heyobi.apk
```

Bei einem Bundle (`.apkm`, `.xapk`, mehrere Splits) entpacken und alle Teile
gemeinsam installieren:

```bash
adb install-multiple base.apk split_config.*.apk
```

---

## 7. Mitschneiden

1. App starten, mit deinem echten OBI-Konto einloggen.
2. In mitmweb den Filter `~d obi` setzen (oder direkt
   `energy-tracking-backend`), damit nur die relevanten Flows bleiben.
3. **Erst jetzt** die Live-Ansicht in der App öffnen und laufen lassen, bis
   Werte kommen.
4. Live-Ansicht wieder schließen — auch das Beenden ist interessant.

### Worauf es ankommt

| Gesucht | Warum |
| --- | --- |
| Der Request, der die Session **startet** (meist POST/PUT, mit Body) | Den muss die Integration nachbauen, bevor irgendwas streamt |
| Was danach kommt: wiederholte GETs oder **ein** dauerhafter Flow | Polling → einfach. WebSocket/MQTT → anderer Bauplan |
| Bei Polling: der **Abstand** zwischen den Requests | Legt das sinnvolle Update-Intervall fest |
| Der `Accept`-Header | Trägt die Version des Media-Types, z. B. `...live-record.v1+json` |
| Ob ein **Keepalive** nötig ist | Sonst stirbt die Session nach ein paar Minuten |
| Der Request beim **Schließen** | Damit die Integration das Dongle wieder schlafen legt |

Eine lange offene Verbindung zu Port 8883, die mitmproxy nur durchreicht und
nicht entschlüsselt, ist MQTT. Dann läuft der Stream über einen Push-Kanal
und die Integration bräuchte einen MQTT-Client statt des Coordinators.

---

## Wenn es klemmt

| Symptom | Ursache / Abhilfe |
| --- | --- |
| `PANIC: Cannot find AVD system path` | `ANDROID_HOME` / `ANDROID_SDK_ROOT` nicht gesetzt |
| `adbd cannot run as root in production builds` | Playstore-Image erwischt — `google_apis`-Image nehmen |
| `adb remount` schlägt fehl | `avbctl disable-verification`, siehe Schritt 5 |
| App: „keine Verbindung" | Proxy nicht aktiv → manuell auf `10.0.2.2:8080` (Schritt 4) |
| In mitmweb nur TLS-Handshake-Fehler der App | Certificate Pinning. Dann APK mit `npx apk-mitm` patchen und neu installieren, oder Frida einsetzen |
| Login scheitert, aber HTTP ist sichtbar | Backend prüft womöglich die App-Integrität → echtes Gerät nötig |
| Live-Ansicht startet im Emulator nicht | Falls die App dafür Bluetooth zum Dongle braucht, ist der Emulator raus — dann echtes Handy mit gepatchter APK |

---

## Vor dem Teilen schwärzen

Der Mitschnitt enthält deinen Account. Vor dem Posten in einem Issue
entfernen: `Authorization: Bearer ...`, das JWT selbst, `accountId`,
Bridge- und Device-IDs, Name, Adresse, E-Mail.

Gebraucht werden nur: Methode, Pfad (IDs durch `{bridge}` / `{device}`
ersetzen), Query-Parameter, `Accept`-Header, Request-Body und ein
Beispiel-Response.
