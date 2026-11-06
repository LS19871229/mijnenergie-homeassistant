# Mijn Energie voor Home Assistant

Stuurt elke minuut de standen van de P1-meter, de omvormer en eventueel gas en thuisbatterij naar het [Mijn Energie-portaal](https://eltrading.nl/mijnenergie) van EL trading. Alles gaat via de schermen van Home Assistant, zonder YAML.

- Koppelen met een 6-cijferige koppelcode uit het beheer
- Stelt zelf de juiste sensoren voor (HomeWizard P1, de meeste omvormers)
- Rekent eenheden zelf om (W/kW, Wh/kWh/MWh, m³)
- Geen internet? Metingen worden tot 7 dagen bewaard en daarna alsnog verstuurd
- Meldingen onder Instellingen › Reparaties als er iets mis is
- Nederlands, met Engels als reserve

Vereist Home Assistant 2025.1 of nieuwer.

## Installeren (eenmalig per HA Green)

1. Installeer [HACS](https://hacs.xyz/docs/use/) als dat nog niet is gebeurd.
2. HACS › menu rechtsboven › **Aangepaste repositories**
   - Repository: `https://github.com/LS19871229/mijnenergie-homeassistant`
   - Type: **Integratie**
3. Zoek in HACS op **Mijn Energie** en klik op **Downloaden**.
4. Herstart Home Assistant.

## Koppelen

1. In het beheer van het portaal: open de woning en klik op **Koppelcode maken**.
2. In Home Assistant: Instellingen › Apparaten en diensten › **Integratie toevoegen** › **Mijn Energie**.
3. Typ de koppelcode in.
4. Controleer de voorgestelde sensoren:
   - **P1-meter:** vermogen, afname, teruglevering en gas
   - **Omvormer:** actueel vermogen en de **totale** opbrengst
   - **Thuisbatterij:** optioneel
5. Er gaat direct een testmeting naar het portaal. In het beheer staat de woning nu op Online.

Later iets wijzigen kan via de integratie › **Configureren**. Daar staat ook **Opnieuw koppelen** (met een nieuwe koppelcode).

## Wat je ziet in Home Assistant

Een apparaat "Mijn Energie · *woning*" met vier diagnosesensoren:

| Sensor | Betekenis |
|---|---|
| Status | Verbonden, Geen verbinding, Opnieuw koppelen nodig of Opstarten |
| Laatste verzending | Wanneer het portaal voor het laatst metingen ontving |
| Wachtende metingen | Metingen die nog verstuurd moeten worden |
| Gekoppelde woning | Naam van de woning in het portaal |

## Problemen oplossen

| Wat je ziet | Oplossing |
|---|---|
| "Deze code is verlopen of al gebruikt" | Maak in het beheer een nieuwe koppelcode (1 uur geldig, één keer bruikbaar). |
| "Het portaal is niet bereikbaar" | Controleer de internetverbinding van de HA Green. |
| Melding "Opnieuw koppelen" | Het portaal accepteert het token niet meer, bijvoorbeeld na "Koppeling intrekken". Maak een nieuwe koppelcode en vul die in bij de melding. Wachtende metingen blijven bewaard. |
| Melding "sensor niet gevonden" | Een gekozen sensor bestaat niet meer, bijvoorbeeld omdat de omvormer opnieuw is toegevoegd. Kies een nieuwe via Configureren. Hernoemde sensoren worden automatisch gevolgd. |
| Woning offline in het portaal | Kijk naar de sensor Status. Is die "Geen verbinding", dan ligt het aan internet. |

Voor support: integratie › menu › **Diagnose downloaden**. Het token staat daar niet in.

## Hoe het werkt

- Elke minuut op seconde 0 leest de integratie de gekozen sensoren. Daarvan maakt hij één meting (vermogen in W, energie in kWh, gas in m³).
- De meting gaat in een wachtrij. Die wordt in pakketjes van maximaal 500 verstuurd naar `api/ingest.php`, met het token in de header `X-Mijnenergie-Token`.
- De wachtrij wordt opgeslagen in `.storage`, ook bij een herstart. Om het geheugen van de HA Green te sparen gebeurt dat maximaal eens per 5 minuten.
- Na 7 dagen zonder verbinding vervallen de oudste metingen.
- Koppelen gaat via `api/pair.php`: de koppelcode wordt ingewisseld voor een token. Een nieuwe koppeling maakt het vorige token ongeldig.

## Voor ontwikkelaars

### Bestanden

```
custom_components/mijnenergie/
  __init__.py      opstarten, stoppen, hernoemde sensoren volgen
  config_flow.py   koppelschermen, Configureren, opnieuw koppelen
  sender.py        meten, wachtrij, versturen, status en meldingen
  measure.py       eenheden omrekenen en meting opbouwen (zonder HA, los te testen)
  detect.py        sensoren voorstellen
  api.py           verbinding met het portaal
  sensor.py        de vier statussensoren
  diagnostics.py   diagnosebestand
  strings.json     teksten (Engels); translations/nl.json en en.json
```

### Testen

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest -q
```

GitHub controleert elke wijziging en elke maandag automatisch. Dat zijn de tests, hassfest (de officiële controle van Home Assistant) en de HACS-controle. Breekt een nieuwe Home Assistant-versie iets, dan zie je dat daar als eerste.

### Nieuwe versie uitbrengen

1. Pas de code aan en zorg dat de tests slagen.
2. Verhoog `version` in `custom_components/mijnenergie/manifest.json` (bijv. `1.0.1`).
3. Maak op GitHub een **Release** met tag `v1.0.1`. Een controle bewaakt dat de tag en de manifest-versie gelijk zijn.
4. Alle HA Greens krijgen de update als melding in HACS.

Nieuwe teksten? Zet ze in `strings.json` én in `translations/en.json` en `translations/nl.json`.
