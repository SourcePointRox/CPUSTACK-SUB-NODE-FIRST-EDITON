import { Navigate, Route, Routes } from 'react-router-dom';
import BasicLayout from './layouts/BasicLayout';
import Login from './pages/Login';
import Dashboard from './pages/Dashboard';
import Workers from './pages/Workers';
import Models from './pages/Models';
import Instances from './pages/Instances';
import Playground from './pages/Playground';
import APIKeys from './pages/APIKeys';
import Settings from './pages/Settings';
import Usage from './pages/Usage';
import Knowledge from './pages/Knowledge';
import { useAuth } from './store/auth';

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { isAuthed } = useAuth();
  if (!isAuthed) {
    return <Navigate to="/login" replace />;
  }
  return <>{children}</>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route
        path="/"
        element={
          <RequireAuth>
            <BasicLayout />
          </RequireAuth>
        }
      >
        <Route index element={<Navigate to="/dashboard" replace />} />
        <Route path="dashboard" element={<Dashboard />} />
        <Route path="workers" element={<Workers />} />
        <Route path="models" element={<Models />} />
        <Route path="instances" element={<Instances />} />
        <Route path="playground" element={<Playground />} />
        <Route path="api-keys" element={<APIKeys />} />
        <Route path="usage" element={<Usage />} />
        <Route path="knowledge" element={<Knowledge />} />
        <Route path="settings" element={<Settings />} />
        <Route path="*" element={<Navigate to="/dashboard" replace />} />
      </Route>
    </Routes>
  );
}
