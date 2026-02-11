# Examen de `mon py` (Flask/Stripe/Postgres)

## Résumé rapide

Le code est globalement structuré et couvre beaucoup de cas (Stripe, blocage essai, démo, leads, crawl). En revanche, il reste quelques incohérences techniques qui peuvent créer des bugs en prod.

## Points critiques repérés

1. **Ordre de collecte inversé dans `rule_based_next_question()`**
   - Le commentaire annonce `Nom -> Téléphone -> Email`, mais la logique actuelle fait d’abord **Téléphone**, puis Nom.
   - Pour respecter l’ordre attendu : tester `name` avant `phone`.

2. **`/api/bettybot` renvoie `stage` au lieu de `effective_stage`**
   - Le commentaire dit que `effective_stage` doit être renvoyé.
   - Le JSON final renvoie actuellement `"stage": stage`.
   - Si `lead` contient des infos contradictoires, le front peut afficher un état faux.

3. **Incohérence schéma `demo_sessions` (`id` vs `session_id`)**
   - `ensure_demo_tables()` crée `demo_sessions(id TEXT PRIMARY KEY, ...)`.
   - `db_demo_ensure_session()` tente `INSERT INTO demo_sessions (session_id, ...)`.
   - Cette fonction plantera dès qu’elle est appelée (colonne inexistante).

4. **Deux imports `datetime` différents**
   - En haut : `from datetime import datetime, timezone`.
   - Plus bas : `from datetime import datetime`.
   - Ce n’est pas bloquant mais c’est confus et source d’erreurs de maintenance.

5. **`enforce_single_question()` est un no-op**
   - La fonction retourne simplement `text` sans traitement.
   - Si l’intention est d’imposer une seule question, il faut implémenter la contrainte ou supprimer cette couche.

## Correctifs recommandés (priorité)

1. Corriger l’ordre dans `rule_based_next_question()`.
2. Retourner `effective_stage` dans la réponse `/api/bettybot`.
3. Uniformiser la clé de session (`id` partout, ou `session_id` partout) dans les fonctions démo.
4. Nettoyer les imports redondants.
5. Soit implémenter `enforce_single_question()`, soit la retirer pour éviter une fausse impression de garde-fou.

## Vérifications conseillées

- Test API `/api/bettybot` :
  - cas 1: nom seul -> demande téléphone
  - cas 2: nom+tel -> demande email
  - cas 3: nom+tel+email -> `stage=ready`
- Test DB démo : création session + insertion messages.
- Test webhook Stripe : `checkout.session.completed`, `invoice.paid`, `invoice.payment_failed`, `customer.subscription.deleted`.
