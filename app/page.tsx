import IntentBaoApp from "./intent-bao-app";
import LoginGate from "./login-gate";

export default function Home() {
  return (
    <LoginGate>
      <IntentBaoApp />
    </LoginGate>
  );
}
