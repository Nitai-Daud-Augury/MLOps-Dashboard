import { Copy, ExternalLink, X } from 'lucide-react';

import type { ManifestResult } from '../../types';
import { formatNumber } from '../../utils/format';

export function ManifestCreatedDialog({ manifest, onClose, onCopy }: { manifest: ManifestResult; onClose: () => void; onCopy: () => void }) {
  return <div className="modal-backdrop" role="presentation" onMouseDown={onClose}><section className="manifest-dialog" role="dialog" aria-modal="true" aria-labelledby="manifest-created-title" onMouseDown={(event) => event.stopPropagation()}>
    <div className="panel-header"><div><p className="eyebrow">Manifest Created</p><h2 id="manifest-created-title">{formatNumber(manifest.rows)} manifest row{manifest.rows === 1 ? '' : 's'} ready</h2></div><button className="icon-button" type="button" onClick={onClose} title="Close manifest dialog"><X size={18} /></button></div>
    <dl><dt>Account</dt><dd className="mono">{manifest.account_name}</dd><dt>Container</dt><dd className="mono">{manifest.container_name}</dd><dt>Blob path</dt><dd className="mono break">{manifest.manifest_path}</dd></dl>
    <div className="actions dialog-actions"><button className="primary-button" type="button" onClick={onCopy}><Copy size={16} /><span>Copy Manifest Blob Path</span></button><a className="secondary-button" href={manifest.blob_url} target="_blank" rel="noreferrer"><ExternalLink size={16} /><span>View Blob on Azure</span></a></div>
  </section></div>;
}
