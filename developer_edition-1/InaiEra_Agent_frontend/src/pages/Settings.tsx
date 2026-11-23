import { PageHead } from '../components/ui';
import GitIntegrations from '../components/GitIntegrations';

// Developer Edition: Settings holds Git integrations only. The read-only
// "Runtime configuration" view (DB pool, UCS, Temporal, model provider) was
// removed; those values are set in docker-compose.yml / .env.
export default function Settings() {
  return (
    <div className="wrap settings-page">
      <PageHead title="Settings" eyebrow="Platform" subtitle="Git integrations for your agents." />
      <GitIntegrations />
    </div>
  );
}
