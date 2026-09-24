import { useState, type MouseEvent } from 'react';
import { Copy } from 'lucide-react';

export function CopyMachineIdButton({ machineId, onCopied }: { machineId: string; onCopied?: () => void }) {
  const [copied, setCopied] = useState(false);
  const copy = async (event: MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    await navigator.clipboard.writeText(machineId);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1800);
    onCopied?.();
  };
  return <button className="copy-machine-id" type="button" onClick={(event) => void copy(event)} title="Copy machine ID" aria-label={`Copy machine ID ${machineId}`}><Copy size={14} /><span>{copied ? 'Copied' : 'Copy'}</span></button>;
}
