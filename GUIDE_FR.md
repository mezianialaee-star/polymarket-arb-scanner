# Guide (pour toi, Alae)

## 1. Où en est le projet

Le code est sur GitHub et tourne tout seul : un scan toutes les 10 minutes (onglet **Actions** → `scan`),
les résultats arrivent dans le dossier `data/`. Tu n'as rien à faire.

## 2. Vérifier que tout marche (quand tu veux)

Onglet **Actions** → **verify** → **Run workflow**. Après ~10 minutes :
- coche verte = tous les tests passent ET le scanner lit correctement les vraies données de Polymarket ;
- croix rouge = clique dessus, copie le texte en rouge et envoie-le-moi.

## 3. Mi-décembre : les résultats

**Actions** → **analyze** → **Run workflow**. Le dossier `report/` contient les chiffres
(`report.md`) et les graphiques. On les met ensemble dans ton CV.

## 4. L'expliquer en 30 secondes

> « Dans un marché à plusieurs issues sur Polymarket, une seule issue gagne. Acheter toutes les issues
> rapporte donc exactement 1 $. Si on peut les acheter toutes pour moins de 1 $, frais compris, c'est un
> profit sans risque. J'ai construit un scanner qui surveille les ~6 000 marchés de ce type toutes les
> 10 minutes, calcule la taille optimale de ce panier et mesure combien ces opportunités rapportent et
> combien de temps elles durent. C'est la même logique que l'arbitrage d'ETF. »

Les questions qu'on peut te poser, et la réponse :
- **Pourquoi c'est sans risque ?** Une seule issue gagne : le panier YES paie toujours 1 $,
  le panier NO toujours N − 1 $.
- **Comment tu trouves la taille optimale ?** Le coût marginal d'un panier de plus ne fait que monter
  quand on remonte les carnets, donc le profit est concave : on achète tant que le panier suivant coûte
  moins que ce qu'il rapporte.
- **Comment tu scannes 65 000 issues toutes les 10 minutes ?** En deux étapes. D'abord seulement les
  meilleurs prix de tout le monde (~260 requêtes). Un panier ne peut être rentable que si la somme des
  meilleurs prix est sous son paiement (les frais et les niveaux plus profonds ne font qu'ajouter du coût) :
  c'est une condition nécessaire, donc on élimine presque tout sans rien rater. Ensuite on lit les carnets
  complets des quelques candidats seulement.
- **Pourquoi l'opportunité existe ?** Il faut acheter plusieurs jambes en même temps, avec des frais et
  des minimums d'ordre : peu de gens le font, et elle disparaît vite.

## 5. Sur ton CV (à compléter avec les vrais chiffres)

*Polymarket arbitrage scanner (Python, GitHub Actions) — Monitored ~6,000 multi-outcome markets
(65,000 outcomes) every 10 min for X weeks with a two-stage screen; detected Y riskless basket
opportunities worth $Z after fees (median duration W min).*

Dans les compétences : `Python (basic)`.
