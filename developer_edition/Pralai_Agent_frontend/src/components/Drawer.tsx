import Icon from './Icon';

export default function Drawer({
  title, onClose, children, footer,
}: { title: React.ReactNode; onClose: () => void; children: React.ReactNode; footer?: React.ReactNode }) {
  return (
    <div className="overlay" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="drawer">
        <div className="dh">
          <h3>{title}</h3>
          <button className="x" onClick={onClose}><Icon name="i-x" /></button>
        </div>
        <div className="db">{children}</div>
        {footer && <div className="df">{footer}</div>}
      </div>
    </div>
  );
}
