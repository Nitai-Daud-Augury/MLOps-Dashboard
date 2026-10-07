type Props = { isTest?: boolean };

export function TestMachineBadge({ isTest }: Props) {
  return isTest
    ? <span className="test-machine-badge" title="Test machine; identified from inventory metadata or the explicit registry" aria-label="Test machine">TEST</span>
    : null;
}
