// Build only: the third-party licence notices (THIRD_PARTY_LICENSES.md at the repository root,
// written by scripts/third_party_licenses.py) travel with every build, beside index.html. A build
// without the file fails rather than ship code whose notices are missing.
import { readFileSync } from 'node:fs';

export const NOTICE = 'THIRD_PARTY_LICENSES.md';

export function thirdPartyLicenses(source = new URL(`../${NOTICE}`, import.meta.url)) {
  return {
    name: 'healthos-third-party-licenses',
    apply: 'build',
    generateBundle() {
      this.emitFile({ type: 'asset', fileName: NOTICE, source: readFileSync(source, 'utf8') });
    },
  };
}
