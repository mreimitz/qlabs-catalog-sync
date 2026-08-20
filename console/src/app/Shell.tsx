import { useMemo } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { LogOut } from "lucide-react";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
  Button,
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarTrigger,
  ThemeSwitcher,
  TopNav,
} from "@elabs-ai/components-ui";
import { AppIcon } from "@elabs-ai/components-icons";

import { signOut } from "../auth/authApi";
import { useSession } from "../auth/useSession";
import { DEFAULT_ROUTE, NAV_ROUTES } from "./routes";

/** The enterprise-admin shell (archetype B, `app-spec.md`): icon-collapsible primary
 * sidebar for the five top-level views, a top bar with breadcrumb + theme switcher +
 * session/sign-out control, and the active screen rendered through `<Outlet/>`. Mounted as
 * the layout route in `App.tsx`, so it is only ever reached once `AuthGate` has confirmed a
 * live session. */
export function Shell() {
  const session = useSession();
  const location = useLocation();

  const activeRoute = useMemo(
    () => NAV_ROUTES.find((route) => location.pathname.startsWith(route.path)),
    [location.pathname],
  );

  const username = session.status === "signed-in" ? session.username : null;

  return (
    <SidebarProvider>
      <Sidebar collapsible="icon">
        <SidebarHeader className="px-3 py-2">
          <AppIcon title="QLabs Catalog Sync" height={20} />
        </SidebarHeader>
        <SidebarContent>
          <SidebarGroup>
            <SidebarGroupContent>
              <SidebarMenu>
                {NAV_ROUTES.map((route) => {
                  const Icon = route.icon;
                  return (
                    <SidebarMenuItem key={route.path}>
                      <SidebarMenuButton asChild isActive={activeRoute?.path === route.path} tooltip={route.label}>
                        <NavLink to={route.path}>
                          <Icon aria-hidden />
                          <span>{route.label}</span>
                        </NavLink>
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  );
                })}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        </SidebarContent>
      </Sidebar>
      <SidebarInset>
        <TopNav
          start={
            <div className="flex items-center gap-2">
              <SidebarTrigger />
              <Breadcrumb>
                <BreadcrumbList>
                  <BreadcrumbItem>
                    <BreadcrumbLink asChild>
                      <NavLink to={DEFAULT_ROUTE}>QLabs Catalog Sync</NavLink>
                    </BreadcrumbLink>
                  </BreadcrumbItem>
                  {activeRoute ? (
                    <>
                      <BreadcrumbSeparator />
                      <BreadcrumbItem>
                        <BreadcrumbPage>{activeRoute.label}</BreadcrumbPage>
                      </BreadcrumbItem>
                    </>
                  ) : null}
                </BreadcrumbList>
              </Breadcrumb>
            </div>
          }
          end={
            <div className="flex items-center gap-3">
              <ThemeSwitcher />
              {username ? <span className="text-caption text-muted-foreground">{username}</span> : null}
              <Button variant="ghost" size="sm" onClick={() => void signOut()}>
                <LogOut aria-hidden className="mr-1.5 size-4" />
                Sign out
              </Button>
            </div>
          }
        />
        {/* NOT a second <main> (brand-ui issue 386, see the T13.1 scaffold): SidebarInset
            already renders the page's <main> landmark, so the content region here is a plain
            <div>. */}
        <div className="p-6">
          <Outlet />
        </div>
      </SidebarInset>
    </SidebarProvider>
  );
}
