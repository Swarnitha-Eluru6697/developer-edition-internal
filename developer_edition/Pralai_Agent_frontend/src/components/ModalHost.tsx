import { useApp } from '../context/AppContext';
import { P } from '../permissions';
import NewAgentModal from '../modals/NewAgentModal';
import EditAgentModal from '../modals/EditAgentModal';
import GenerateAgentModal from '../modals/GenerateAgentModal';
import PublishModal from '../modals/PublishModal';
import AddMcpModal from '../modals/AddMcpModal';
import ApiKeyConfigModal from '../modals/ApiKeyConfigModal';
import AddProviderModelModal from '../modals/AddProviderModelModal';
import EditKbConnectionModal from '../modals/EditKbConnectionModal';
import DsConfigModal from '../modals/DsConfigModal';
import AddEnvVarModal from '../modals/AddEnvVarModal';
import AddKnowledgeBaseModal from '../modals/AddKnowledgeBaseModal';
import NewRagPipelineModal from '../modals/NewRagPipelineModal';

export default function ModalHost() {
  const { modal, modalArg, can } = useApp();
  if (!modal) return null;
  switch (modal) {
    case 'newAgent': return <NewAgentModal />;
    case 'editAgent': return modalArg ? <EditAgentModal agentId={modalArg} /> : null;
    case 'generateAgent': return <GenerateAgentModal />;
    // Publish, unpublish and UCS grants all live in this modal.
    case 'publish': return modalArg && can(P.agentsPublish) ? <PublishModal agentId={modalArg} /> : null;
    case 'addMcp': return <AddMcpModal />;
    case 'apiKeyConfig': return modalArg ? <ApiKeyConfigModal provider={modalArg} /> : null;
    case 'addProviderModel': return modalArg ? <AddProviderModelModal provider={modalArg} /> : null;
    case 'editKbConnection': return modalArg ? <EditKbConnectionModal kbId={modalArg} /> : null;
    case 'dsConfig': return modalArg ? <DsConfigModal sourceName={modalArg} /> : null;
    case 'addEnvVar': return modalArg ? <AddEnvVarModal agentId={modalArg} /> : null;
    case 'addKnowledgeBase': return <AddKnowledgeBaseModal />;
    case 'newRagPipeline': return <NewRagPipelineModal />;
    default: return null;
  }
}
