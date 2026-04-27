import {
  useState,
  useEffect,
  useCallback,
  type FormEvent,
} from "react";
import {
  listUsers,
  createUser,
  updateUser,
  deleteUser,
} from "@/lib/api/apis/AdminApi";
import type { AdminUser } from "@/lib/api/apis/AdminApi";
import type { UserInfo } from "@/lib/api/apis/AuthApi";
import { AdminPluginsPanel } from "./admin-plugins-panel";
import { AdminBrandingPanel } from "./admin-branding-panel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
  DialogDescription,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { ArrowLeft, Loader2, Plus, RefreshCw, Trash2, KeyRound, ToggleLeft, ToggleRight, Users, Puzzle, Palette } from "lucide-react";
import { toast } from "sonner";

type AdminTab = "users" | "plugins" | "branding";

type AdminPageProps = {
  currentUser: UserInfo;
};

function formatDate(timestamp: number): string {
  return new Date(timestamp * 1000).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

type CreateUserDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: () => void;
};

function CreateUserDialog({ open, onOpenChange, onCreated }: CreateUserDialogProps) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("user");
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleClose = useCallback(() => {
    setUsername("");
    setPassword("");
    setRole("user");
    setError(null);
    onOpenChange(false);
  }, [onOpenChange]);

  const handleSubmit = useCallback(
    async (e?: FormEvent) => {
      e?.preventDefault();
      const trimmed = username.trim();
      if (!trimmed) {
        setError("Username is required.");
        return;
      }
      if (!password) {
        setError("Password is required.");
        return;
      }
      setError(null);
      setIsLoading(true);
      try {
        await createUser(trimmed, password, role);
        toast.success("User created successfully");
        onCreated();
        handleClose();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to create user.");
      } finally {
        setIsLoading(false);
      }
    },
    [username, password, role, onCreated, handleClose],
  );

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Create New User</DialogTitle>
          <DialogDescription>
            Add a new user account. The user will be able to log in immediately.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4 pt-2">
          <div className="flex flex-col gap-1.5">
            <label htmlFor="new-username" className="text-sm font-medium text-foreground">
              Username
            </label>
            <Input
              id="new-username"
              type="text"
              placeholder="Enter username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              disabled={isLoading}
              autoFocus
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <label htmlFor="new-password" className="text-sm font-medium text-foreground">
              Password
            </label>
            <Input
              id="new-password"
              type="password"
              placeholder="Enter password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={isLoading}
            />
          </div>

          <div className="flex flex-col gap-1.5">
            {/* biome-ignore lint/a11y/noLabelWithoutControl: Select component wraps the control */}
            <label className="text-sm font-medium text-foreground">Role</label>
            <Select value={role} onValueChange={setRole} disabled={isLoading}>
              <SelectTrigger className="w-full">
                <SelectValue placeholder="Select role" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="user">User</SelectItem>
                <SelectItem value="admin">Admin</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {error && (
            <p className="text-sm text-destructive" role="alert">
              {error}
            </p>
          )}

          <DialogFooter>
            <Button type="button" variant="outline" onClick={handleClose} disabled={isLoading}>
              Cancel
            </Button>
            <Button type="submit" disabled={isLoading}>
              {isLoading ? <><Loader2 className="animate-spin" />Creating...</> : "Create User"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

type ResetPasswordDialogProps = {
  user: AdminUser | null;
  onOpenChange: (open: boolean) => void;
  onUpdated: () => void;
};

function ResetPasswordDialog({ user, onOpenChange, onUpdated }: ResetPasswordDialogProps) {
  const [newPassword, setNewPassword] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleClose = useCallback(() => {
    setNewPassword("");
    setError(null);
    onOpenChange(false);
  }, [onOpenChange]);

  const handleSubmit = useCallback(
    async (e?: FormEvent) => {
      e?.preventDefault();
      if (!user) return;
      if (!newPassword) {
        setError("New password is required.");
        return;
      }
      setError(null);
      setIsLoading(true);
      try {
        await updateUser(user.id, { password: newPassword });
        toast.success(`Password reset for ${user.username}`);
        onUpdated();
        handleClose();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to reset password.");
      } finally {
        setIsLoading(false);
      }
    },
    [user, newPassword, onUpdated, handleClose],
  );

  return (
    <Dialog open={!!user} onOpenChange={handleClose}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Reset Password</DialogTitle>
          <DialogDescription>
            Set a new password for{" "}
            <strong>{user?.username}</strong>.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4 pt-2">
          <div className="flex flex-col gap-1.5">
            <label htmlFor="reset-password" className="text-sm font-medium text-foreground">
              New Password
            </label>
            <Input
              id="reset-password"
              type="password"
              placeholder="Enter new password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              disabled={isLoading}
              autoFocus
            />
          </div>

          {error && (
            <p className="text-sm text-destructive" role="alert">
              {error}
            </p>
          )}

          <DialogFooter>
            <Button type="button" variant="outline" onClick={handleClose} disabled={isLoading}>
              Cancel
            </Button>
            <Button type="submit" disabled={isLoading}>
              {isLoading ? <><Loader2 className="animate-spin" />Resetting...</> : "Reset Password"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function AdminPage({ currentUser }: AdminPageProps) {
  const [activeTab, setActiveTab] = useState<AdminTab>("users");

  const [users, setUsers] = useState<AdminUser[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const [resetPasswordUser, setResetPasswordUser] = useState<AdminUser | null>(null);
  const [deleteTargetUser, setDeleteTargetUser] = useState<AdminUser | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);
  const [togglingUserId, setTogglingUserId] = useState<string | null>(null);

  const loadUsers = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const data = await listUsers();
      setUsers(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load users.");
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    loadUsers();
  }, [loadUsers]);

  const handleToggleActive = useCallback(
    async (user: AdminUser) => {
      setTogglingUserId(user.id);
      try {
        const updated = await updateUser(user.id, { is_active: !user.is_active });
        setUsers((prev) => prev.map((u) => (u.id === updated.id ? updated : u)));
        toast.success(
          updated.is_active
            ? `${user.username} has been enabled`
            : `${user.username} has been disabled`,
        );
      } catch (err) {
        toast.error(err instanceof Error ? err.message : "Failed to update user.");
      } finally {
        setTogglingUserId(null);
      }
    },
    [],
  );

  const handleDeleteConfirm = useCallback(async () => {
    if (!deleteTargetUser) return;
    setIsDeleting(true);
    try {
      await deleteUser(deleteTargetUser.id);
      setUsers((prev) => prev.filter((u) => u.id !== deleteTargetUser.id));
      toast.success(`${deleteTargetUser.username} has been deleted`);
      setDeleteTargetUser(null);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to delete user.");
    } finally {
      setIsDeleting(false);
    }
  }, [deleteTargetUser]);

  const totalUsers = users.length;
  const activeUsers = users.filter((u) => u.is_active).length;

  return (
    <div className="min-h-[100dvh] bg-background text-foreground overflow-y-auto">
      <div className="mx-auto max-w-5xl px-4 py-6">
        {/* Header */}
        <div className="mb-4 flex items-center gap-4">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => { window.location.href = "/"; }}
            className="gap-2"
          >
            <ArrowLeft className="size-4" />
            Back
          </Button>
          <h1 className="text-xl font-semibold">Admin Panel</h1>
          {activeTab === "users" && (
            <div className="ml-auto flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={loadUsers}
                disabled={isLoading}
                className="gap-2"
              >
                <RefreshCw className={isLoading ? "animate-spin size-4" : "size-4"} />
                Refresh
              </Button>
              <Button size="sm" onClick={() => setShowCreateDialog(true)} className="gap-2">
                <Plus className="size-4" />
                New User
              </Button>
            </div>
          )}
        </div>

        {/* Tab nav */}
        <div className="mb-6 flex gap-1 rounded-lg border bg-muted/40 p-1 w-fit">
          <button
            type="button"
            onClick={() => setActiveTab("users")}
            className={[
              "inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
              activeTab === "users"
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            ].join(" ")}
          >
            <Users className="size-3.5" />
            Users
          </button>
          <button
            type="button"
            onClick={() => setActiveTab("plugins")}
            className={[
              "inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
              activeTab === "plugins"
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            ].join(" ")}
          >
            <Puzzle className="size-3.5" />
            Plugins
          </button>
          <button
            type="button"
            onClick={() => setActiveTab("branding")}
            className={[
              "inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
              activeTab === "branding"
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            ].join(" ")}
          >
            <Palette className="size-3.5" />
            Branding
          </button>
        </div>

        {/* Plugins tab */}
        {activeTab === "plugins" && <AdminPluginsPanel />}

        {/* Branding tab */}
        {activeTab === "branding" && <AdminBrandingPanel />}

        {/* Users tab */}
        {activeTab === "users" && (
          <div className="flex flex-col gap-6">
            {/* Stats */}
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
              <Card>
                <CardHeader className="pb-1">
                  <CardTitle className="text-sm font-medium text-muted-foreground">
                    Total Users
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <p className="text-3xl font-bold">{totalUsers}</p>
                </CardContent>
              </Card>
              <Card>
                <CardHeader className="pb-1">
                  <CardTitle className="text-sm font-medium text-muted-foreground">
                    Active Users
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <p className="text-3xl font-bold text-green-600 dark:text-green-400">
                    {activeUsers}
                  </p>
                </CardContent>
              </Card>
              <Card>
                <CardHeader className="pb-1">
                  <CardTitle className="text-sm font-medium text-muted-foreground">
                    Inactive Users
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <p className="text-3xl font-bold text-muted-foreground">
                    {totalUsers - activeUsers}
                  </p>
                </CardContent>
              </Card>
            </div>

            {/* Error */}
            {error && (
              <div className="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive">
                {error}
              </div>
            )}

            {/* Users table */}
            <div className="rounded-xl border bg-card shadow-sm overflow-hidden">
          {isLoading && users.length === 0 ? (
            <div className="flex items-center justify-center py-16 text-muted-foreground gap-2">
              <Loader2 className="animate-spin size-4" />
              Loading users...
            </div>
          ) : users.length === 0 ? (
            <div className="flex items-center justify-center py-16 text-muted-foreground">
              No users found.
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b bg-muted/40 text-left text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    <th className="px-4 py-3">Username</th>
                    <th className="px-4 py-3">Role</th>
                    <th className="px-4 py-3">Status</th>
                    <th className="px-4 py-3">Sessions</th>
                    <th className="px-4 py-3">Created</th>
                    <th className="px-4 py-3 text-right">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {users.map((user) => {
                    const isSelf = user.id === currentUser.user_id;
                    const isToggling = togglingUserId === user.id;

                    return (
                      <tr
                        key={user.id}
                        className="transition-colors hover:bg-muted/30"
                      >
                        <td className="px-4 py-3 font-medium">
                          {user.username}
                          {isSelf && (
                            <span className="ml-2 text-xs text-muted-foreground">(you)</span>
                          )}
                        </td>
                        <td className="px-4 py-3">
                          <Badge
                            variant={user.role === "admin" ? "default" : "secondary"}
                          >
                            {user.role}
                          </Badge>
                        </td>
                        <td className="px-4 py-3">
                          {user.is_active ? (
                            <Badge variant="outline" className="border-green-500/40 text-green-600 dark:text-green-400 bg-green-500/10">
                              Active
                            </Badge>
                          ) : (
                            <Badge variant="outline" className="text-muted-foreground">
                              Inactive
                            </Badge>
                          )}
                        </td>
                        <td className="px-4 py-3 text-muted-foreground">
                          {user.session_count}
                        </td>
                        <td className="px-4 py-3 text-muted-foreground">
                          {formatDate(user.created_at)}
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex items-center justify-end gap-1">
                            {/* Reset password */}
                            <Button
                              variant="ghost"
                              size="icon-sm"
                              title="Reset password"
                              onClick={() => setResetPasswordUser(user)}
                            >
                              <KeyRound className="size-3.5" />
                            </Button>

                            {/* Toggle active/inactive */}
                            <Button
                              variant="ghost"
                              size="icon-sm"
                              title={user.is_active ? "Disable user" : "Enable user"}
                              disabled={isSelf || isToggling}
                              onClick={() => handleToggleActive(user)}
                            >
                              {isToggling ? (
                                <Loader2 className="size-3.5 animate-spin" />
                              ) : user.is_active ? (
                                <ToggleRight className="size-3.5 text-green-600 dark:text-green-400" />
                              ) : (
                                <ToggleLeft className="size-3.5 text-muted-foreground" />
                              )}
                            </Button>

                            {/* Delete */}
                            <Button
                              variant="ghost"
                              size="icon-sm"
                              title="Delete user"
                              disabled={isSelf}
                              className="text-muted-foreground hover:text-destructive"
                              onClick={() => setDeleteTargetUser(user)}
                            >
                              <Trash2 className="size-3.5" />
                            </Button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
            </div>
          </div>
        )}
      </div>

      {/* Create user dialog */}
      <CreateUserDialog
        open={showCreateDialog}
        onOpenChange={setShowCreateDialog}
        onCreated={loadUsers}
      />

      {/* Reset password dialog */}
      <ResetPasswordDialog
        user={resetPasswordUser}
        onOpenChange={(open) => {
          if (!open) setResetPasswordUser(null);
        }}
        onUpdated={loadUsers}
      />

      {/* Delete confirm dialog */}
      <AlertDialog
        open={!!deleteTargetUser}
        onOpenChange={(open) => {
          if (!open) setDeleteTargetUser(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete User</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to delete{" "}
              <strong>{deleteTargetUser?.username}</strong>? This action cannot
              be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isDeleting}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-white hover:bg-destructive/90"
              disabled={isDeleting}
              onClick={handleDeleteConfirm}
            >
              {isDeleting ? (
                <><Loader2 className="animate-spin size-4" />Deleting...</>
              ) : (
                "Delete"
              )}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
