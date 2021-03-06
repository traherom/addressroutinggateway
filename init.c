#ifdef HAVE_CONFIG_H
#include "config.h"
#endif

#include <stdio.h>
#include <time.h>

#include "settings.h"
#include "hopper.h"
#include "director.h"
#include "arg_error.h"
#include "nat.h"

// Signal handler
#ifdef HAVE_SIGNAL_H
#include <signal.h>

void sig_handler(int signum)
{
	uninit_director();
}
#endif

// Called when the module is initialized
static int arg_init(char *configPath, char *gateName)
{
	struct config_data conf;
	time_t rawtime;
	struct tm * timeinfo;
	char buf[30];

	time(&rawtime);
	timeinfo = localtime(&rawtime);
	strftime(buf, sizeof(buf), "%d %b %Y %T", timeinfo);	
	arglog(LOG_DEBUG, "Starting at %s\n", buf);

	// Take care of locks first so that we know they're ALWAYS safe to use
	init_nat_locks();
	init_hopper_locks();
	init_protocol_locks();
	init_director_locks();

	// Read in main config
	strncpy(conf.file, configPath, sizeof(conf.file) - 1);
	if(read_config(&conf))
	{
		arglog(LOG_ALERT, "Unable to read in main configuration from %s\n", configPath);
		return -ARG_CONFIG_BAD;
	}

	// For testing, the command line overrides what the config says our name is
	if(gateName != NULL)
		strncpy(conf.ourGateName, gateName, sizeof(conf.ourGateName) - 1);

	// Init various components
	if(init_hopper(&conf))
	{
		arglog(LOG_DEBUG, "Unable to initialize hopper\n");
		
		uninit_hopper();
		
		return -ARG_CONFIG_BAD;
	}

	if(init_nat())
	{
		arglog(LOG_DEBUG, "NAT failed to initialize\n");

		uninit_nat();
		uninit_hopper();

		return -ARG_CONFIG_BAD;
	}

	// Hook network communication to listen for instructions
	if(init_director(&conf))
	{
		arglog(LOG_DEBUG, "Director failed to initialized, disabling subsystems\n");
		
		uninit_director();
		uninit_nat();
		uninit_hopper();
		
		return -ARG_CONFIG_BAD;
	}

	arglog(LOG_DEBUG, "Running\n");
   
	// Do first attempt to connect to the gateways we know of
	init_hopper_finish();
	
	// No more need for config
	release_config(&conf);

	return 0;
}

// Called when the module is unloaded
static void arg_exit(void)
{
	arglog(LOG_DEBUG, "Shutting down\n");

	// Unregister our network hooks so the system doesn't crash
	uninit_director();

	// Cleanup any resources as needed
	uninit_nat();
	uninit_hopper();
	
	arglog(LOG_DEBUG, "Finished\n");
}

int main(int argc, char *argv[])
{
	#ifdef HAVE_SIGNAL_H
	// Set up signal handler
	struct sigaction action;
	action.sa_handler = sig_handler;
	sigemptyset (&action.sa_mask);
	action.sa_flags = 0;

	sigaction (SIGINT, &action, NULL);
	//sigaction (SIGHUP, &action, NULL);
	//sigaction (SIGTERM, &action, NULL);
	#endif

	if(argc != 2 && argc != 3)
	{
		arglog(LOG_DEBUG, "Usage: %s <conf path> [<gate name>]\n", argv[0]);
		return 1;
	}

	if(argc == 2)
		arg_init(argv[1], NULL);
	else
		arg_init(argv[1], argv[2]);
	
	// Run, waiting patiently
	join_director();
	
	arg_exit();
	
	#ifdef HAVE_SIGNAL_H
	// Disconnect handlers
	action.sa_handler = SIG_IGN;
	sigemptyset (&action.sa_mask);
	action.sa_flags = 0;

	sigaction (SIGINT, &action, NULL);
	sigaction (SIGHUP, &action, NULL);
	sigaction (SIGTERM, &action, NULL);
	#endif
	
	return 0;
}

