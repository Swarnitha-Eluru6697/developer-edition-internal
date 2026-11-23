import { useParams } from 'react-router-dom';
import Icon from '../components/Icon';
import { NODE_DOCS } from '../nodeDocs';
import { renderDocsMarkdown } from '../docsMarkdown';
import { NODE_TYPES } from '../constants';

export default function NodeDocs() {
  const { nodeType } = useParams<{ nodeType: string }>();
  const doc = nodeType ? NODE_DOCS[nodeType] : undefined;
  const nt = nodeType ? NODE_TYPES[nodeType] : undefined;

  return (
    <div className="docs-page">
      <div className="docs-page-header">
        <div className="brandbar" style={{ position: 'static' }}>
          <div className="logo">◆</div>
          <div><b>InAiEra Agents</b><br /><span>NODE DOCUMENTATION</span></div>
        </div>
      </div>
      <div className="docs-page-body">
        {!doc ? (
          <div className="empty-cfg" style={{ padding: 60 }}>
            No documentation found for node type "{nodeType}".
          </div>
        ) : (
          <>
            <div className="docs-page-title">
              {nt && <span className="cnode-icon" style={{ background: nt.dot || 'var(--slate)' }}><Icon name={nt.icon} /></span>}
              <div>
                <h1>{doc.title}</h1>
                <p className="docs-page-summary">{doc.summary}</p>
              </div>
            </div>
            <div className="docs-page-content" dangerouslySetInnerHTML={{ __html: renderDocsMarkdown(doc.body) }} />
          </>
        )}
      </div>
    </div>
  );
}
