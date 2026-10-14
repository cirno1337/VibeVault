# Adapter fixtures

* `<site>/real-<case>-<yyyymm>.html`: real page saves, scrubbed with `scripts/scrub-fixture.mjs`. The structure
  is real; the text is pseudo-random with the same length and shape. Only a few UI phrases are kept verbatim,
  so tests can assert those never leak into message text.
* `<site>/synthetic-*.html`: hand-written in the same shape, for cases the real saves lack (streaming, edited
  prompt, regenerated answer, empty page, math).

## Adding a real fixture (when a site changes its layout)

1. Open the conversation and wait for the answer to finish. In DevTools → Console, run:
   ```js
   const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([document.documentElement.outerHTML],{type:'text/html'}));a.download=location.hostname+'-'+Date.now()+'.html';a.click()
   ```
2. Move the file into `_raw/`. That folder is gitignored, because the file contains your conversation.
3. Run `node scripts/scrub-fixture.mjs _raw/<file> <site>/real-<case>-<yyyymm>.html` (paths relative to this
   folder). Then grep the output for a few words from the conversation before committing.
4. Add a test. Keep the old fixtures: they document the previous layout.
