import Icon from './Icon';
import { useApp } from '../context/AppContext';

export default function ToastHost() {
  const { toasts } = useApp();
  if (!toasts.length) return null;
  const t = toasts[toasts.length - 1];
  return (
    <div className="toast">
      {/* The check glyph is wrong for a failure, and a blocked publish
          is the most common thing this shows: it reported "Publish
          blocked — …" next to a tick. Chosen from the text because
          toasts are plain strings here, with no severity of their own. */}
      <Icon name={/blocked|failed|could not|cannot|error|refused/i.test(t.text) ? 'i-alert' : 'i-check'} />
      <span style={{ flex: 1, minWidth: 0 }}>{t.text}</span>
    </div>
  );
}
