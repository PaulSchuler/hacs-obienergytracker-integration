# Live-Mode-API (aus der heyOBI-App extrahiert)

Ergebnis der statischen Analyse von `de.obi.app` 26.9.2 (XAPK, `classes*.dex`,
dekompiliert mit jadx). Der Live-Modus der App läuft **nicht** über die
`historical-data`-Endpunkte, die diese Integration bisher kennt, sondern über
einen eigenen WebSocket.

Die Angaben stammen aus dem Bytecode und wurden am 22.09.2026 gegen das
echte Backend verifiziert — mit einem laufenden Live-Stream aus einem realen
Konto. Was noch offen ist, steht unten.

---

## Überblick

Der Ablauf besteht aus zwei Teilen: Das Sensor-Upload-Intervall wird per REST
heruntergesetzt, dann liefert ein WebSocket die Messwerte.

```
1.  PATCH  https://api.obi.com/energytracker/api/sensors/{sensorId}
             -> uploadInterval herabsetzen
2.  GET    ws://api.obi.com/energytracker/api-livemode/retrieving
             ?bridgeId={bridgeId}&sensorId={sensorId}
             -> Messwerte empfangen
3.  PATCH  https://api.obi.com/energytracker/api/sensors/{sensorId}
             -> Intervall zurücksetzen
```

Schritt 3 steht so nicht im Code, ist aber dringend zu empfehlen: Der Sensor
ist batteriebetrieben, und ein dauerhaft kurzes Upload-Intervall leert die
Batterie entsprechend schneller.

## Hosts und Pfade

Die App benutzt in Version 26.9.2 **`api.obi.com`**, nicht den
`energy-tracking-backend.prod-eks.dbs.obi.solutions`, den diese Integration
noch anspricht. Die REST-Endpunkte liegen dort hinter dem Pfad-Präfix
`energytracker/api`; der Live-WebSocket hängt daneben, nicht darunter:

| Zweck | URL |
| --- | --- |
| REST (neu) | `https://api.obi.com/energytracker/api/...` |
| REST (alt) | `https://energy-tracking-backend.prod-eks.dbs.obi.solutions/...` |
| Live-Socket | `ws://api.obi.com/energytracker/api-livemode/retrieving` |

Geprüft: `GET /users/{accountId}` antwortet auf `api.obi.com` ohne das Präfix
mit 404, mit Präfix mit 401 (also Route vorhanden, Token abgelehnt) — und mit
echtem Token mit 200.

Die Integration spricht seit der Live-Mode-Erweiterung primär `api.obi.com`
an und fällt bei einem 404 automatisch auf den alten Host zurück; der einmal
erfolgreiche Host wird für die restliche Sitzung gemerkt. Die
`historical-data`-Endpunkte sind auf dem neuen Host **noch nicht** verifiziert
— dafür gibt es ja den Rückfall.

| Umgebung | Host |
| --- | --- |
| prod | `api.obi.com` |
| stage | `stage.api.obi.com` / `internal.stage.api.obi.com` |
| dev | `dev.api.obi.com` / `internal.dev.api.obi.com` |

*(dekompiliert: `defpackage/bi6.java`, Präfix aus `defpackage/ai6.java` case 2)*

## Login

Unverändert gegenüber der Integration — gleicher Pfad, gleicher Body:

```http
POST /regi/auth/api/public/login
Content-Type: application/json

{"email": "...", "password": "...", "country": "DE"}
```

Die Antwort enthält `token`. **Der Host ist aber marktabhängig**: Die App
wählt ihn nach dem Land des Kontos, und der `country`-Wert im Body muss dazu
passen. Bekannte Märkte: `www.obi.de` (DE), `www.obi.at` (AT), `www.obi.ch`
(CH), `www.obi.cz` (CZ), `www.obi.hu` (HU), `www.obi.pl` (PL), `www.obi.si`
(SI), `www.obi.sk` (SK).

Ein falscher Markt liefert HTTP 401 ohne Fehlertext — nicht von falschen
Zugangsdaten zu unterscheiden. Die App kennt außerdem Google- und
Apple-Anmeldung; Konten, die daran hängen, haben kein Passwort für diesen
Endpunkt.

*(dekompiliert: `defpackage/b6j.java`, `defpackage/djj.java`,
`de/obi/app/data/auth/model/LoginData$$serializer`)*

## 1. Live-Modus einschalten

```http
PATCH /energytracker/api/sensors/{sensorId} HTTP/1.1
Host: api.obi.com
Authorization: Bearer <JWT>
Accept: application/vnd.obi.companion.energy-tracking.sensor.v2+json
Content-Type: application/vnd.obi.companion.energy-tracking.sensor.v2+json

{"id": "<sensorId>", "uploadInterval": <int>}
```

Für Steckdosen statt Zählersensoren lautet der Pfad `/outlets/{outletId}` und
der Media-Type `application/vnd.obi.companion.energy-tracking.outlet.v1+json`.

Der aktuelle Wert von `uploadInterval` steht im Sensor-Objekt unter
`/energytracker/api/users/{accountId}` → `bridge.sensors[]`, zusammen mit
`id`, `isOnline`, `batteryLevel`, `hardwareVersion`, `firmwareVersion`, `otaProgress`,
`otaStatus`, `displayName`, `claimedAt`, `dataVisibleSince`.

`uploadInterval` ist in **Sekunden**. Die App setzt beim Betreten der
Live-Ansicht **2** und beim Verlassen **300**:

```java
le6Var.Y(str, er5Var, 2,   this)   // Live an
le6Var.Y(str, er5Var, 300, this)   // Live aus
```

Werte unter 2 werden mit `HTTP 400 {"statusCode":400,"message":"Bad Request
Exception"}` abgelehnt — verifiziert mit `1`.

**Wichtig für die Batterie:** Die App setzt den Wert beim Schließen nicht
immer zurück. In freier Wildbahn wurde ein Sensor mit `uploadInterval=2`
angetroffen, der also dauerhaft alle zwei Sekunden sendet. Wer den Wert
wiederherstellt, darf deshalb nicht blind das vorgefundene Original nehmen —
sonst zementiert er den Live-Wert. Die Werkzeuge hier setzen auf 300, wenn
das Original unter 60 liegt.

*(Aufrufstelle: `defpackage/r8c.java`)*

*(dekompiliert: `defpackage/y8c.java`, Methode `b`; `uk9.e` = PATCH;
`heyobi/domain/client/energy_tracking/SensorUploadIntervalV1DTO$$serializer`)*

## 2. WebSocket

```
GET ws://api.obi.com/energytracker/api-livemode/retrieving
      ?bridgeId=<bridgeId>&sensorId=<sensorId>
Authorization: Bearer <JWT>
```

Für Steckdosen wird statt `sensorId` der Parameter `outletId` gesetzt.

Die Authentifizierung läuft über den Ktor-Auth-Plugin im Bearer-Modus, der
Token geht also als normaler `Authorization`-Header mit dem
Upgrade-Request raus.

**Es muss `wss://` sein.** Der Bytecode setzt zwar `ws` (Ktor
`URLProtocol.createOrDefault("ws")`, Port 80), aber das Backend antwortet
darauf mit HTTP 400. Über `wss://` auf 443 kommt der Handshake sauber
zustande. Warum die App damit durchkommt, ist unklar — möglicherweise greift
ein Redirect, den Ktor selbst auflöst.

Bei einem fehlgeschlagenen Handshake wirft die App
`Handshake failed with HTTP <code>` für Status 400–499.

*(dekompiliert: `defpackage/y8c.java` Methode `a` → `defpackage/rco.java`
Methode `j` → `defpackage/ct.java` case 8 (Protokoll, Host, Pfad) und
`defpackage/pqb.java` case 3 (Query-Parameter); Host aus
`defpackage/ej6.java:118`, Ktor-Client `live_mode_ws_client`)*

## 3. Nachrichtenformat

Die Frames sind JSON und werden mit `SensorLiveMessageV1DTO` deserialisiert.
Alle Felder sind nullable:

```json
{"event":"mqttMessage","data":{"rssi":-75,"power":506,"battery":56}}
```

Echte Frames aus einem laufenden Stream. `event` war in allen beobachteten
Nachrichten `mqttMessage` — das Backend reicht also offenbar MQTT-Nachrichten
der Bridge durch.

| Feld | Typ | Bedeutung |
| --- | --- | --- |
| `event` | String? | beobachtet: immer `mqttMessage` |
| `data.power` | Double? | Momentanleistung in Watt |
| `data.rssi` | Int? | Funkempfangsstärke des Sensors |
| `data.battery` | Int? | Batteriestand |

Für Steckdosen gilt `OutletLiveMessageV1DTO` / `OutletLiveDataV1DTO` mit
demselben Aufbau.

Ob Einspeisung als negativer `power`-Wert kommt oder gar nicht, ist offen —
im Screenshot der Live-Ansicht gibt es getrennte Anzeigen für „Max.
Verbrauch" und „Max. Einspeisung", was für ein Vorzeichen im selben Feld
spricht.

*(dekompiliert: `heyobi/domain/client/energy_tracking/SensorLiveDataV1DTO$$serializer`
und `SensorLiveMessageV1DTO$$serializer`)*

## Bestätigt

- Der Token aus `www.obi.de/regi/auth/api/public/login` wird von
  `api.obi.com` akzeptiert.
- `wss://` funktioniert, `ws://` nicht.
- `uploadInterval`: Sekunden, 2 = live, 300 = normal, Minimum 2.
- Frames: `{"event":"mqttMessage","data":{"rssi":…,"power":…,"battery":…}}`.

- **Der PATCH allein genügt.** Mit `uploadInterval=2` liefert der Socket
  Frames, ohne dass die App geöffnet sein muss. Die App ist nur deshalb
  nötig gewesen, weil der erste Versuch mit `1` abgelehnt wurde.
- **Der Wert muss exakt 2 sein.** Weder kleiner noch größer funktioniert;
  es ist also kein Bereich, sondern ein Schalter.

## Offen

- Ob der Server die Verbindung von sich aus beendet und ob ein Keepalive
  nötig ist. Beobachtet wurde ein Abriss, nachdem die App geschlossen wurde.
- Ob Einspeisung als negativer `power`-Wert kommt.
- Wie stark ein Intervall von 2 Sekunden die Sensorbatterie belastet.
