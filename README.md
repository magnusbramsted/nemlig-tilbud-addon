# Nemlig tilbudshenter

Home Assistant-add-on, der prissætter dine Mealie-retter med Nemligs aktuelle priser
(inkl. prismatch med REMA 1000) og samler ugens tilbud.

## Hvad den laver

Hver kørsel (standard: én gang i døgnet):

1. Henter alle opskrifter fra Mealie (mængder, enheder, portioner).
2. Læser Nemligs tilbudsside og skriver `nemlig_tilbud.json`, `nemlig_gratis.json`,
   `nemlig_madspild.json` og `nemlig_store_rabatter.json` i `/share`.
3. Søger hver fødevare op hos Nemlig, vælger den billigste passende vare pr. kg/l/stk.
   og beregner for hver ret:
   - **kurvpris**: hele pakninger, der skal i kurven
   - **forbrugspris**: kun den mængde, retten bruger
   - begge pr. portion

   Resultat: `/share/nemlig_retpriser.json` (oversigt, sorteret billigst først) og
   `/share/nemlig_retpriser_detaljer.json` (alle ingredienser og valgte varer).

Basisvarer (salt, peber, olie, tørrede krydderier) tæller ikke med.

## Ret matchningen

Standardreglerne ligger i `nemlig_tilbud/app/ingredients.json`. Egne regler lægges i
`/share/nemlig_ingredienser.json` og overskriver standarden pr. fødevare, fx:

```json
{
  "bacon": {"query": "bacon tern", "require": ["bacon"], "exclude": ["skiver"]},
  "hakket oksekød": {"product_id": "5012345"},
  "kokosmælk": {"pantry": true}
}
```

| Felt | Betydning |
|---|---|
| `query` | Søgeord hos Nemlig |
| `require` / `require_any` | Ord, der skal stå i varenavnet (alle / mindst ét) |
| `exclude` | Ord, der udelukker en vare |
| `exact` | Kræv hele ord (fx `løg` må ikke matche `løgringe`) |
| `density` | g pr. ml, til omregning af dl/spsk til vægt |
| `g_per_piece` | Vægt pr. stk., fx løg = 130 |
| `units` | Fødevarespecifikke enheder, fx `{"fed": {"g": 5}}` |
| `pantry` | Basisvare, udelades |
| `product_id` | Lås fødevaren til en bestemt Nemlig-vare |
| `same_as` | Brug en anden fødevares regel |

Fødevarer i detaljefilen med `"status": "mangler"` viser `search_top`, altså de første
søgeresultater, så man kan se, hvorfor intet matchede.

## Udvikling

```bash
python3 -m pytest tests/
```
