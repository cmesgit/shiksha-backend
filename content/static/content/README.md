# Vendored assets for the demo-video upload widget

`tus.min.js` is **tus-js-client 4.3.1**, copied verbatim from
`shiksha-teacher-dashboard/node_modules/tus-js-client/dist/tus.min.js`. It is
the same version the teacher dashboard bundles for its own Bunny uploads
(`src/shared/bunnyUpload.js`), so both upload paths behave identically.

It is checked in rather than fetched from a CDN because this runs inside the
Django admin, which has no build step and no npm dependency of its own — and
because an upload widget that silently stops working when a third-party CDN is
unreachable is worse than one large file in the repo.

To update it, copy a newer `dist/tus.min.js` over this one and re-run the
upload once against a real Bunny library. Keep it in step with the teacher
dashboard's version; a divergence here means a bug can reproduce on one upload
path and not the other, which is a miserable thing to debug.
