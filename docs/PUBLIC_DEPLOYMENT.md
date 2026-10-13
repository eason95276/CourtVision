# Public deployment

Local generation → saved projection → GitHub → Actions → Pages.
Pages fits native HTML/CSS/JS, hash routes and JSON, with free HTTPS/custom domain.
Vercel/backend hosting is unnecessary.

Website: https://eason95276.github.io/CourtVision/
Repository: https://github.com/eason95276/CourtVision

## Publish from the private workspace

After local generation and formal publication succeed:

```sh
python publish_game.py espn:GAME_ID --destination ../courtvision-public/public_data
cd ../courtvision-public
python scripts/check_public_release.py --history
python -m unittest discover -s tests/public -v
python scripts/build_public_site.py
git add public_data
git commit -m "Publish saved game"
git push origin main
```

The public exporter defaults to its own `public_data`. It reads saved GET endpoints,
never generation POST. Private API is needed only during export. Updating public
projection never mutates source versions. Do not automatically repackage source on
every game; source releases need review. Do not stage the private workspace.

Pages uses GitHub Actions: history/tree scan → focused checks → build `_site` →
upload → deploy. Failed builds leave previous deployment intact. Game hash routes
such as `#game/espn%3A401898390` survive refresh. No backend is hosted.

## Custom domain: weitao.si

This domain did not point to Pages at release time. No premature redirect is added.
Do not put registrar credentials/cookies in source.

1. Prefer verify ownership in account **Settings → Pages**, using GitHub's generated
   TXT record (do not invent a verification value).
2. In repo **Settings → Pages → Custom domain**, add `weitao.si`.
3. In registrar DNS, replace conflicting parking A records for `@`:

   | Type | Host | Value |
   |---|---|---|
   | A | @ | 185.199.108.153 |
   | A | @ | 185.199.109.153 |
   | A | @ | 185.199.110.153 |
   | A | @ | 185.199.111.153 |
   | CNAME | www | eason95276.github.io |

4. After DNS/certificate checks, enable **Enforce HTTPS**. Verify root/WWW and game
   refresh. Coordinate steps: incomplete binding may redirect to an unready domain.

Official: [custom workflows](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages),
[domain setup](https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site/managing-a-custom-domain-for-your-github-pages-site).

Not deployed: Agent/API/database, LLM keys, Research, Scheduler, Judge, Human workspace,
tracing services or private ground truth.
