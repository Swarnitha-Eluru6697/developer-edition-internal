import { useApp } from '../context/AppContext';
import AgentProfileDrawer from '../drawers/AgentProfileDrawer';
import RunDetailDrawer from '../drawers/RunDetailDrawer';
import TraceDetailDrawer from '../drawers/TraceDetailDrawer';
import AlertDetailDrawer from '../drawers/AlertDetailDrawer';
import ToolDetailDrawer from '../drawers/ToolDetailDrawer';
import EnvVarsDrawer from '../drawers/EnvVarsDrawer';

export default function DrawerHost() {
  const { drawer, drawerArg } = useApp();
  if (!drawer || !drawerArg) return null;
  switch (drawer) {
    case 'agentProfile': return <AgentProfileDrawer agentId={drawerArg} />;
    case 'runDetail': return <RunDetailDrawer runId={drawerArg} />;
    case 'traceDetail': return <TraceDetailDrawer traceId={drawerArg} />;
    case 'alertDetail': return <AlertDetailDrawer alertId={drawerArg} />;
    case 'toolDetail': return <ToolDetailDrawer toolName={drawerArg} />;
    case 'envVars': return <EnvVarsDrawer agentId={drawerArg} />;
    default: return null;
  }
}
