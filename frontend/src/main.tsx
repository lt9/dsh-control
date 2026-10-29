import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router-dom";
import "./styles.css";
import App from "./App";
import ListPage from "./pages/ListPage";
import DetailPage from "./pages/DetailPage";
import NewPage from "./pages/NewPage";
import SettingsPage from "./pages/SettingsPage";
import JobPage from "./pages/JobPage";
import MemoryPage from "./pages/MemoryPage";
import CapabilityPage from "./pages/CapabilityPage";

const router = createBrowserRouter([
  { path: "/login", element: <App /> },
  {
    element: <App />, children: [
      { path: "/", element: <ListPage /> },
      { path: "/instances/new", element: <NewPage /> },
      { path: "/instances/:id", element: <DetailPage /> },
      { path: "/jobs/:id", element: <JobPage /> },
      { path: "/memory", element: <MemoryPage /> },
      { path: "/capabilities", element: <CapabilityPage /> },
      { path: "/settings", element: <SettingsPage /> },
      { path: "*", element: <Navigate to="/" replace /> },
    ],
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode><RouterProvider router={router} /></StrictMode>,
);
