import Icon from "./Icon.jsx";

export default function Brand() {
  return (
    <div className="brand">
      <span className="brand-mark">
        <Icon name="shield" />
      </span>
      <span>
        Event Review<small>Supervisor workspace</small>
      </span>
    </div>
  );
}
