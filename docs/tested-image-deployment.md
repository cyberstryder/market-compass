# Build once, test, promote by digest

Quality gates continue to run the complete PostgreSQL test suite, JavaScript checks,
and Docker-packaged tests. A new smoke check starts the resulting container as its
configured runtime user and requires `/health` to respond successfully. No tests
are removed from the Dockerfile.

On `main`, the tested image is saved as a one-day Actions artifact and loaded into
a separate publication job with package-write permissions. PR code only receives
read permissions. The publisher **does not rebuild**. It pushes the loaded image
to private `ghcr.io/cyberstryder/market-compass`, captures its immutable digest and
saves a 90-day `image-release.json` manifest. Tags include source SHA, run ID and
attempt, but production uses `@sha256:...`, never a floating tag.

Once activated, the deployment job sets that exact digest on dashboard, collector
and engine in that order. Each service must pass Railway's existing `/health`
check and return the same image in its deployment metadata before the next moves.
A receipt records previous image digests, deployment IDs and partial results.
Credentials, unrelated services, commands, variables, networking and replica topology are not changed.
Concurrent promotions serialize; superseded commits, active deployments, mutable
images, missing health checks and unconfigured source
cutovers fail before promotion. A failed or uncertain deployment stops the rollout;
it is not blindly retried or automatically rolled back.

## One-time activation

The repository is private. Keep the image private as well. Connected app access
is not a substitute for registry credentials or a CI deployment token.

1. In Railway project **Market Compass → Settings → Tokens**, create a project
   token scoped to **production**. Save it in GitHub repository **Settings →
   Secrets and variables → Actions** as the secret `RAILWAY_TOKEN`. Do not commit
   it, paste it in chat, or add it to a release manifest.
2. Merge the workflow PR. The `main` workflow tests and publishes the first image.
   Open the `publish` job summary or download `image-release.json` for its digest.
   The existing GitHub-connected Railway path remains intact until source cutover;
   this transition merge may therefore cause one final source build.
3. For each of **dashboard, collector, engine**, configure Railway Source as that
   exact `ghcr.io/cyberstryder/market-compass@sha256:...` image and disconnect the
   GitHub repository source. Preserve each service's variables, role, command,
   `/health` check and networking. Under Source → Registry Credentials, add a
   GitHub personal access token (classic) with `read:packages` access to this
   private package. Railway requires its Pro plan for private registries. Use
   Railway's secure credential field; never make the package public as a shortcut.
4. Verify all three initial image deployments succeed. In GitHub Actions repository
   **Variables**, set `RAILWAY_IMAGE_DEPLOY_ENABLED` to `true`. Keep the `production`
   Actions environment's chosen review protections. The next main run (or a manual
   workflow dispatch on main) then promotes automatically, with no Railway build.

Until these credentials and source connections are configured, CI prepares images
but production **continues using the old deployment path**. An enabled deployment
job with missing setup fails clearly rather than silently claiming a release.

## Verification and rollback

- The same digest must appear in `image-release.json`, all three Railway image
  sources and their successful deployment metadata.
- Confirm public `/health` and authenticated dashboard functions after cutover;
  inspect engine/collector health. Do not send Discord test messages or orders.
- Store the initial cutover source settings. Later deployment receipts retain each
  previous image digest. To roll back, select the previous successful deployment
  in Railway or restore its pinned image, and verify health. Do not rerun a stale
  main workflow: the revision guard intentionally rejects superseded commits.
- Never delete image versions still referenced by production or its rollback
  deployments. Image artifact retention and GHCR image retention are different.

References: [Railway private registries](https://docs.railway.com/builds/private-registries),
[Railway service API](https://docs.railway.com/integrations/api/manage-services),
[GitHub Container registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).
