# Bot SIG Predictions Cup

Bot qui passe par l'API officielle du jeu. Le règlement l'autorise (section 6).

**Stratégie (sans risque).** Le jeu a un marché par parti pour la même élection, par exemple « Will the Republican Party win the Texas Senate? » et « Will the Democratic Party win the Texas Senate? ». Un seul des deux peut gagner. Donc acheter NO sur les deux rapporte au moins 1 par lot. Quand la somme des meilleurs prix d'achat YES (S) dépasse 1, un lot coûte moins de 1 : le gain garanti est d'au moins S − 1 par lot. Le bot passe un seul ordre « tout ou rien » : soit tout est acheté au bon prix, soit rien.

**Mise en route**
1. Sur le jeu : My Profile → API Keys → Create New API Key. Coche `read` et `trade`, choisis 30 jours, puis copie la clé.
2. Sur GitHub : Settings → Secrets and variables → Actions → New repository secret. Nom : `SIG_API_KEY`, valeur : la clé.
3. Le bot démarre tout seul dans l'onglet Actions (workflow `sig-bot`), d'abord en **test** (`MODE: "dry"`) : il n'achète rien et note ce qu'il aurait fait.
4. Pour trader pour de vrai : `MODE: "live"` et `LIVE_CONFIRM: "yes"` dans `.github/workflows/sig-bot.yml`.

**Limites** (dans le workflow) : 3 000 maximum par opération, 10 000 maximum par session, 100 de cash toujours gardés. Le bot s'arrête seul le 4 novembre à 16h45 UTC, avant la fin du jeu.

**Lancer à la main** (sinon, le bot démarre tout seul toutes les 30 minutes) : https://github.com/mezianialaee-star/polymarket-arb-scanner/actions/workflows/sig-bot.yml → Run workflow → Run workflow.

**Suivre** : l'état du bot est enregistré toutes les 10 minutes dans `sig_bot/status/latest.md`.

**Arrêter** : Actions → sig-bot → « ... » → Disable workflow.

Tests hors ligne : `cd sig_bot && python3 -m unittest -v test_sig_bot`
