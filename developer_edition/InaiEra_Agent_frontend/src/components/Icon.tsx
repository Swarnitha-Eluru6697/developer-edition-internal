export default function Icon({ name, className, style }: { name: string; className?: string; style?: React.CSSProperties }) {
  return (
    <svg className={className} style={style}>
      <use href={`#${name}`} />
    </svg>
  );
}
