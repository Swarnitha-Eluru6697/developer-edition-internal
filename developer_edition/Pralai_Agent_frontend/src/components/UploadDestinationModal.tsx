import { useEffect, useState } from 'react';
import Icon from './Icon';
import Modal from './Modal';
import { api } from '../api';
import { useApp } from '../context/AppContext';
import type { StorageTarget, StorageDestination } from '../types';

/** Where uploaded files are kept — chosen per upload, not fixed in .env.
 *
 *  Two ways out, which is the whole point of the dialog: keep using the
 *  destination already configured, or enter different credentials. New
 *  credentials are TESTED against the bucket before anything is saved,
 *  so a destination that cannot be written to never makes it into the
 *  list, and the failure shows up here rather than at the end of
 *  somebody's upload.
 *
 *  New credentials are stored (encrypted) rather than used once and
 *  forgotten, and that is deliberate: the platform has to reach these
 *  objects again later — to serve the download link on a document, to
 *  delete the object when its document is deleted, and to sweep it at
 *  the end of its retention window. Credentials that disappeared with
 *  the request would make all three impossible.
 */
export default function UploadDestinationModal({ target, onClose, onChosen }: {
  target: StorageTarget;
  onClose: () => void;
  /** The destination id to use for the next upload. */
  onChosen: (destinationId: string, refreshed: StorageTarget) => void;
}) {
  const { toast } = useApp();
  const [destinations, setDestinations] = useState<StorageDestination[]>(target.destinations || []);
  const [selected, setSelected] = useState<string>(target.destination_id || 'env');
  const [mode, setMode] = useState<'existing' | 'new'>('existing');

  const [name, setName] = useState('');
  const [bucket, setBucket] = useState('');
  const [region, setRegion] = useState('');
  const [endpointUrl, setEndpointUrl] = useState('');
  const [accessKeyId, setAccessKeyId] = useState('');
  const [secretAccessKey, setSecretAccessKey] = useState('');
  const [makeDefault, setMakeDefault] = useState(true);

  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  // The result of the last real write/read/delete against the bucket.
  // Kept separate from a generic "saved" toast because it is evidence,
  // not a status: it says what was actually done.
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null);

  useEffect(() => { setDestinations(target.destinations || []); }, [target]);

  const runTest = () => {
    if (!bucket.trim()) { toast('Enter a bucket name first'); return; }
    setTesting(true);
    setTestResult(null);
    api.testStorageDestination({
      bucket: bucket.trim(),
      region: region.trim() || undefined,
      endpoint_url: endpointUrl.trim() || undefined,
      access_key_id: accessKeyId.trim() || undefined,
      secret_access_key: secretAccessKey.trim() || undefined,
    })
      .then(setTestResult)
      .catch((e) => setTestResult({ ok: false, message: e.message }))
      .finally(() => setTesting(false));
  };

  const saveNew = () => {
    if (!bucket.trim()) { toast('Enter a bucket name'); return; }
    setSaving(true);
    api.createStorageDestination({
      name: name.trim() || undefined,
      bucket: bucket.trim(),
      region: region.trim() || undefined,
      endpoint_url: endpointUrl.trim() || undefined,
      access_key_id: accessKeyId.trim() || undefined,
      secret_access_key: secretAccessKey.trim() || undefined,
      make_default: makeDefault,
    })
      .then((created) => api.getStorageTarget().then((refreshed) => {
        toast(`Saved ${created.label}`);
        onChosen(created.id, refreshed);
        onClose();
      }))
      .catch((e) => toast(e.message))
      .finally(() => setSaving(false));
  };

  const useExisting = () => {
    const chosen = destinations.find((d) => d.id === selected);
    if (chosen && !chosen.configured) {
      toast('That destination has no bucket configured');
      return;
    }
    // Only refresh the target when the default actually changed; picking
    // a destination for this one upload leaves the default alone.
    api.getStorageTarget()
      .then((refreshed) => { onChosen(selected, refreshed); onClose(); })
      .catch((e) => toast(e.message));
  };

  return (
    <Modal
      title="Where uploaded files are kept"
      sub="Files ingested with “keep the original file” are written here. Existing files stay where they were written and are still read back from there."
      onClose={onClose}
      footer={
        mode === 'existing' ? (
          <>
            <button className="btn ghost" onClick={onClose}>Cancel</button>
            <button className="btn" onClick={useExisting}>Continue with this destination</button>
          </>
        ) : (
          <>
            <button className="btn ghost" onClick={() => setMode('existing')}>Back</button>
            <button className="btn ghost" disabled={testing || !bucket.trim()} onClick={runTest}>
              <Icon name="i-test" /> {testing ? 'Testing…' : 'Test connection'}
            </button>
            <button className="btn" disabled={saving || !bucket.trim()} onClick={saveNew}>
              {saving ? 'Saving…' : 'Save and use'}
            </button>
          </>
        )
      }
    >
      {mode === 'existing' ? (
        <>
          <div className="ds-browser" style={{ marginBottom: 12 }}>
            <ul className="ds-list">
              {destinations.map((d) => (
                <li key={d.id} className="ds-row">
                  <label className="ds-name">
                    <input
                      type="radio"
                      name="upload-destination"
                      checked={selected === d.id}
                      onChange={() => setSelected(d.id)}
                    />
                    <span className="ds-label">
                      <b>{d.label}</b>
                      {d.region ? <span className="small muted"> · {d.region}</span> : null}
                      {d.endpoint_url ? <span className="small muted"> · {d.endpoint_url}</span> : null}
                    </span>
                  </label>
                  <span className="small muted" style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                    {d.is_default && <span className="chip teal">default</span>}
                    {/* Where the credentials come from, because ".env"
                        and "stored here" are changed in different
                        places and by different people. */}
                    <span>{d.editable ? 'stored credentials' : 'server environment'}</span>
                  </span>
                </li>
              ))}
            </ul>
          </div>
          {destinations.find((d) => d.id === selected)?.last_error && (
            <p className="hint" style={{ color: 'var(--danger, #b23b3b)' }}>
              Last test failed: {destinations.find((d) => d.id === selected)!.last_error}
            </p>
          )}
          <button className="btn sm ghost" onClick={() => { setMode('new'); setTestResult(null); }}>
            <Icon name="i-plus" /> Use new credentials
          </button>
          {target.can_store_credentials === false && (
            <p className="hint" style={{ color: 'var(--amber-text, #8a5a00)' }}>
              This server is running on a temporary encryption key, so credentials entered here could
              not be read back after a restart. Set a durable <span className="mono">SECRETS_ENCRYPTION_KEY</span> first.
            </p>
          )}
        </>
      ) : (
        <>
          <div className="field">
            <label htmlFor="dest-bucket">Bucket</label>
            <input id="dest-bucket" placeholder="my-company-uploads" value={bucket}
                   onChange={(e) => { setBucket(e.target.value); setTestResult(null); }} />
          </div>
          <div className="field">
            <label htmlFor="dest-region">Region</label>
            <input id="dest-region" placeholder="us-east-2" value={region}
                   onChange={(e) => { setRegion(e.target.value); setTestResult(null); }} />
          </div>
          <div className="field">
            <label htmlFor="dest-access-key">Access key ID</label>
            <input id="dest-access-key" autoComplete="off" placeholder="AKIA…" value={accessKeyId}
                   onChange={(e) => { setAccessKeyId(e.target.value); setTestResult(null); }} />
            <p className="hint">
              Leave both key fields empty to use whatever credentials the server itself already has
              (an instance or IAM role).
            </p>
          </div>
          <div className="field">
            <label htmlFor="dest-secret">Secret access key</label>
            <input id="dest-secret" type="password" autoComplete="new-password" placeholder="••••••••"
                   value={secretAccessKey}
                   onChange={(e) => { setSecretAccessKey(e.target.value); setTestResult(null); }} />
          </div>
          <div className="field">
            <label htmlFor="dest-endpoint">Endpoint URL</label>
            <input id="dest-endpoint" placeholder="https://s3.eu-central-1.wasabisys.com" value={endpointUrl}
                   onChange={(e) => { setEndpointUrl(e.target.value); setTestResult(null); }} />
            <p className="hint">Only for MinIO, Cloudflare R2, Wasabi and other S3-compatible services. Leave empty for AWS.</p>
          </div>
          <div className="field">
            <label htmlFor="dest-name">Name (optional)</label>
            <input id="dest-name" placeholder="Marketing bucket" value={name}
                   onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="kb-option">
            <div className="kb-option-h">
              <span className="kb-option-title"><b className="small">Use for future uploads too</b></span>
              <label className="switch">
                <input type="checkbox" aria-label="Use for future uploads too"
                       checked={makeDefault} onChange={(e) => setMakeDefault(e.target.checked)} />
                <span />
              </label>
            </div>
            <p className="hint">
              Off means these credentials are saved and used for this upload, while new uploads keep
              going to the current destination.
            </p>
          </div>
          {testResult && (
            <p className="hint" style={{ color: testResult.ok ? 'var(--teal-d)' : 'var(--danger, #b23b3b)' }}>
              {testResult.ok
                ? `Works — ${testResult.message}.`
                : `Failed — ${testResult.message}`}
            </p>
          )}
          <p className="hint">
            Saving tests these credentials first by writing, reading and deleting one small object;
            nothing is stored if that fails. Keys are encrypted at rest and never shown again.
          </p>
        </>
      )}
    </Modal>
  );
}
