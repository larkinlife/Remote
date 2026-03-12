{ pkgs, ... }: {
  channel = "stable-24.11";

  packages = [
    pkgs.tmate
    pkgs.jq
    pkgs.curl
  ];

  idx = {
    workspace = {
      onCreate = {
        bootstrap = "bash scripts/bootstrap.sh";
      };
      onStart = {
        start-services = "bash scripts/start.sh";
      };
    };
  };
}
