# Guide (pour toi, Alae)

## 1. Mettre le projet en ligne (15 min, une seule fois)

1. Crée un compte sur **github.com** (gratuit) et confirme ton e-mail.
2. Clique sur **New repository**. Nom : `polymarket-arb-scanner`. Coche **Public**
   (obligatoire : en public, GitHub Actions est gratuit et illimité). Ne coche rien d'autre. **Create**.
3. Sur la page du dépôt, clique **uploading an existing file**, glisse **tout le contenu** du dossier
   dézippé (y compris le dossier `.github`), puis **Commit changes**.
4. Va dans l'onglet **Actions**. Tu dois voir 3 workflows : `scan`, `verify`, `analyze`.
   Si tu ne les vois pas, le dossier `.github` (caché sur Mac/Windows) n'est pas passé :
   dis-le-moi, je te donne la solution en 2 minutes.

## 2. Vérification en direct (le test qui compte)

Onglet **Actions** → **verify** → **Run workflow**. Après ~2 minutes :
- coche verte = tous les tests passent ET le scanner lit correctement les vraies données de Polymarket ;
- croix rouge = clique dessus, copie le texte de l'étape en rouge et envoie-le-moi.

Ensuite **Actions** → **scan** → **Run workflow** une première fois. Après, il tourne tout seul toutes les
10 minutes et les résultats apparaissent dans le dossier `data/`.

## 3. Mi-décembre : les résultats

**Actions** → **analyze** → **Run workflow**. Le dossier `report/` contient les chiffres
(`report.md`) et les graphiques. On les met ensemble dans ton CV.

## 4. L'expliquer en 30 secondes

> « Dans un marché à plusieurs issues sur Polymarket, une seule issue gagne. Acheter toutes les issues
> rapporte donc exactement 1 $. Si on peut les acheter toutes pour moins de 1 $, frais compris, c'est un
> profit sans risque. J'ai construit un scanner qui lit tous les carnets d'ordres toutes les 10 minutes,
> calcule la taille optimale de ce panier et mesure combien ces opportunités rapportent et combien de
> temps elles durent. C'est la même logique que l'arbitrage d'ETF. »

Les 3 questions qu'on peut te poser, et la réponse :
- **Pourquoi c'est sans risque ?** Une seule issue gagne : le panier YES paie toujours 1 $,
  le panier NO toujours N − 1 $.
- **Comment tu trouves la taille optimale ?** Le coût marginal d'un panier de plus ne fait que monter
  quand on remonte les carnets, donc le profit est concave : on achète tant que le panier suivant coûte
  moins que ce qu'il rapporte.
- **Pourquoi l'opportunité existe ?** Il faut acheter plusieurs jambes en même temps, avec des frais et
  des minimums d'ordre : peu de gens le font, et elle disparaît vite.

## 5. Sur ton CV (à compléter avec les vrais chiffres)

*Polymarket arbitrage scanner (Python, GitHub Actions) — Monitored N multi-outcome markets every
10 min for X weeks; detected Y riskless basket opportunities worth $Z after fees (median duration W min).*

Dans les compétences : `Python (basic)`.
