import Icon from './Icon';

export default function Modal({
  title, sub, onClose, children, footer,
}: { title: React.ReactNode; sub?: string; onClose: () => void; children: React.ReactNode; footer?: React.ReactNode }) {
  return (
    <div className="overlay" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal">
        <div className="dh">
          <h3>{title}</h3>
          <button className="x" onClick={onClose}><Icon name="i-x" /></button>
        </div>
        <div className="db">
          {sub && <p className="muted small" style={{ margin: '0 0 16px' }}>{sub}</p>}
          {children}
        </div>
        {footer && <div className="df">{footer}</div>}
      </div>
    </div>
  );
}
